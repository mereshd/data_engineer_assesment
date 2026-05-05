"""End-to-end pipeline orchestration.

The pipeline:

  1. Walks the input directory in deterministic order.
  2. Classifies each file by extension; unsupported files are recorded but
     not opened.
  3. For supported, non-empty files, runs the matching processor inside a
     ``try/except`` so a single bad file never aborts the run.
  4. Writes sanitized outputs into ``<output_root>/sanitized/`` mirroring
     the input layout.
  5. Emits five artifacts under ``<output_root>/reports/``:
        - ``run_summary.json``        - top-level run telemetry
        - ``file_manifest.jsonl``     - one row per input file
        - ``validation_report.json``  - four post-run integrity checks
        - ``pii_transformations.csv`` - one row per *mapped* PII match
                                        (configured value -> known token)
        - ``pii_quarantine.csv``      - one row per *unmapped* PII match
                                        (regex hit, no config entry yet)

The two PII reports share a flat 8-column schema, so they're emitted as
CSV (operator-friendly: opens in Excel / BI tools / pandas without parsing
JSON). They're separated by file so an operator can hand the quarantine
file to the on-call triage queue without also exposing the (much larger)
full transformation log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import validation
from .deid import STATUS_MAPPED, STATUS_UNMAPPED, DeIdentifier
from .processors import (
    FindingRecord,
    process_csv,
    process_json,
    process_txt_md,
)
from .utils import (
    add_counts,
    empty_replacement_counts,
    ensure_parent,
    iter_input_files,
    make_run_id,
    relative_posix,
    sha256_bytes,
    sha256_file,
    utc_now,
    utc_now_iso,
    write_csv,
    write_json,
    write_jsonl,
)

LOG = logging.getLogger("sanitizer.pipeline")

ProcessorFn = Callable[
    [Path, DeIdentifier],
    "tuple[bytes, int, dict[str, int], list[FindingRecord]]",
]

SUPPORTED_PROCESSORS: dict[str, ProcessorFn] = {
    ".txt": process_txt_md,
    ".md": process_txt_md,
    ".json": process_json,
    ".csv": process_csv,
}

SANITIZED_SUBDIR = "sanitized"
REPORTS_SUBDIR = "reports"

PII_TRANSFORMATIONS_FILENAME = "pii_transformations.csv"
PII_QUARANTINE_FILENAME = "pii_quarantine.csv"

# Fixed column order for both PII CSVs; "file" first so a plain `sort` /
# `uniq -f1` style triage workflow groups records by source file naturally.
PII_FIELDNAMES = [
    "file",
    "kind",
    "value",
    "value_hash",
    "token",
    "status",
    "location",
    "snippet",
]


@dataclass
class RunResult:
    """Lightweight return value from :func:`run` (mirrors the run summary)."""

    run_id: str
    run_status: str
    summary_path: Path
    manifest_path: Path
    validation_path: Path
    pii_transformations_path: Path | None
    pii_quarantine_path: Path | None
    summary: dict[str, Any]
    manifest: list[dict[str, Any]]
    validation: dict[str, Any]
    pii_transformations: list[dict[str, Any]] = field(default_factory=list)
    pii_quarantine: list[dict[str, Any]] = field(default_factory=list)


def run(
    *,
    input_root: Path | str,
    output_root: Path | str,
    config_path: Path | str,
) -> RunResult:
    input_root = Path(input_root)
    output_root = Path(output_root)
    config_path = Path(config_path)

    if not input_root.exists() or not input_root.is_dir():
        raise FileNotFoundError(
            f"Input root does not exist or is not a directory: {input_root}"
        )

    deid = DeIdentifier.from_config_path(config_path)

    started = utc_now()
    run_id = make_run_id(started)
    started_at = started.strftime("%Y-%m-%dT%H:%M:%SZ")

    sanitized_root = output_root / SANITIZED_SUBDIR
    reports_root = output_root / REPORTS_SUBDIR
    sanitized_root.mkdir(parents=True, exist_ok=True)
    reports_root.mkdir(parents=True, exist_ok=True)

    LOG.info("Starting run %s", run_id)
    LOG.info("  input:  %s", input_root)
    LOG.info("  output: %s", output_root)
    LOG.info("  config: %s", config_path)

    manifest: list[dict[str, Any]] = []
    pii_transformations: list[dict[str, Any]] = []
    pii_quarantine: list[dict[str, Any]] = []
    totals = empty_replacement_counts()
    by_extension: dict[str, dict[str, int]] = {}

    for path in iter_input_files(input_root):
        row, file_records = _process_one(
            path, input_root, sanitized_root, deid
        )
        manifest.append(row)
        _log_file_outcome(row)
        for record in file_records:
            if record["status"] == STATUS_MAPPED:
                pii_transformations.append(record)
            elif record["status"] == STATUS_UNMAPPED:
                pii_quarantine.append(record)

        ext = row["extension"]
        ext_bucket = by_extension.setdefault(ext, {})
        ext_bucket[row["status"]] = ext_bucket.get(row["status"], 0) + 1

        add_counts(totals, row["replacements"])

    LOG.info(
        "Discovered %d files (%d processed, %d skipped, %d failed, %d empty)",
        len(manifest),
        sum(1 for r in manifest if r["status"] == "processed"),
        sum(1 for r in manifest if r["status"] == "skipped_unsupported"),
        sum(1 for r in manifest if r["status"] == "failed"),
        sum(1 for r in manifest if r["status"] == "empty"),
    )

    counts = _status_totals(manifest)
    unmapped_totals = _kind_histogram(pii_quarantine)
    run_status = _derive_run_status(counts, unmapped_totals)

    completed_at = utc_now_iso()

    LOG.info("Running validation checks against sanitized outputs...")
    validation_report = validation.run(
        input_root=input_root,
        output_root=output_root,
        manifest_rows=manifest,
    )
    LOG.info(
        "Validation: %s (%d checks)",
        "PASSED" if validation_report["passed"] else "FAILED",
        len(validation_report.get("checks", [])),
    )

    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "input_root": str(input_root),
        "output_root": str(output_root),
        "run_status": run_status,
        "files_discovered": len(manifest),
        "files_processed": counts["processed"],
        "files_skipped_unsupported": counts["skipped_unsupported"],
        "files_failed": counts["failed"],
        "empty_files": counts["empty"],
        "by_extension": _sort_by_extension(by_extension),
        "replacements": totals,
        "unmapped": unmapped_totals,
        "validation": _validation_summary(validation_report),
    }

    summary_path = reports_root / "run_summary.json"
    manifest_path = reports_root / "file_manifest.jsonl"
    validation_path = reports_root / "validation_report.json"
    pii_transformations_path = _write_optional_csv(
        reports_root / PII_TRANSFORMATIONS_FILENAME,
        pii_transformations,
        PII_FIELDNAMES,
    )
    pii_quarantine_path = _write_optional_csv(
        reports_root / PII_QUARANTINE_FILENAME,
        pii_quarantine,
        PII_FIELDNAMES,
    )

    write_json(summary_path, summary)
    write_jsonl(manifest_path, manifest)
    write_json(validation_path, validation_report)

    return RunResult(
        run_id=run_id,
        run_status=run_status,
        summary_path=summary_path,
        manifest_path=manifest_path,
        validation_path=validation_path,
        pii_transformations_path=pii_transformations_path,
        pii_quarantine_path=pii_quarantine_path,
        summary=summary,
        manifest=manifest,
        validation=validation_report,
        pii_transformations=pii_transformations,
        pii_quarantine=pii_quarantine,
    )


# ----------------------------------------------------------------- per-file


def _process_one(
    path: Path,
    input_root: Path,
    sanitized_root: Path,
    deid: DeIdentifier,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Handle a single file; never raises.

    Returns ``(manifest_row, finding_records)`` where each finding record
    already carries the file's relative path (mapped + unmapped, mixed).
    The pipeline splits them by ``status`` after the loop.
    """
    rel = relative_posix(path, input_root)
    ext = path.suffix.lower()

    row: dict[str, Any] = {
        "relative_path": rel,
        "extension": ext,
        "status": "processed",
        "input_sha256": None,
        "output_sha256": None,
        "output_path": None,
        "records_processed": 0,
        "replacements": empty_replacement_counts(),
        "unmapped": {"emails": 0, "phones": 0},
        "error": None,
    }

    try:
        row["input_sha256"] = sha256_file(path)
    except OSError as e:
        row["status"] = "failed"
        row["error"] = f"{type(e).__name__}: {e}"
        return row, []

    processor = SUPPORTED_PROCESSORS.get(ext)
    if processor is None:
        row["status"] = "skipped_unsupported"
        return row, []

    out_rel = f"{SANITIZED_SUBDIR}/{rel}"
    out_path = sanitized_root / rel

    try:
        size = path.stat().st_size
    except OSError as e:
        row["status"] = "failed"
        row["error"] = f"{type(e).__name__}: {e}"
        return row, []

    if size == 0:
        ensure_parent(out_path)
        out_path.write_bytes(b"")
        row["status"] = "empty"
        row["output_path"] = out_rel
        row["output_sha256"] = sha256_bytes(b"")
        return row, []

    try:
        output_bytes, records, counts, file_records = processor(path, deid)
    except Exception as e:  # noqa: BLE001 - per-file failure isolation
        # Per-file logging happens uniformly in _log_file_outcome from the
        # main loop, so we don't double-emit here. Status + serialized
        # error on the manifest row are the source of truth either way.
        row["status"] = "failed"
        row["error"] = f"{type(e).__name__}: {e}"
        return row, []

    ensure_parent(out_path)
    out_path.write_bytes(output_bytes)

    annotated = [{"file": rel, **record} for record in file_records]
    unmapped = {
        "emails": sum(
            1 for r in annotated
            if r["kind"] == "email" and r["status"] == STATUS_UNMAPPED
        ),
        "phones": sum(
            1 for r in annotated
            if r["kind"] == "phone" and r["status"] == STATUS_UNMAPPED
        ),
    }

    row["output_path"] = out_rel
    row["output_sha256"] = sha256_bytes(output_bytes)
    row["records_processed"] = records
    row["replacements"] = counts
    row["unmapped"] = unmapped
    return row, annotated


# ----------------------------------------------------------------- logging


def _log_file_outcome(row: dict[str, Any]) -> None:
    """Emit one log line per file, consistently formatted across statuses.

    Failed files log at WARNING (visible without ``--verbose``) so an
    operator running the CLI without flags still sees which file broke
    and why. Everything else logs at INFO and only shows up under
    ``--verbose``.
    """
    rel = row["relative_path"]
    status = row["status"]
    if status == "processed":
        records = row["records_processed"]
        replacements = sum(row["replacements"].values())
        unmapped = sum(row["unmapped"].values())
        LOG.info(
            "%s -> processed (%d %s, %d %s, %d unmapped %s)",
            rel,
            records, "record" if records == 1 else "records",
            replacements, "replacement" if replacements == 1 else "replacements",
            unmapped, "record" if unmapped == 1 else "records",
        )
    elif status == "skipped_unsupported":
        LOG.info("%s -> skipped_unsupported", rel)
    elif status == "empty":
        LOG.info("%s -> empty (0 bytes)", rel)
    elif status == "failed":
        LOG.warning("%s -> failed (%s)", rel, row["error"])
    else:
        LOG.info("%s -> %s", rel, status)


# ---------------------------------------------------------------- summaries


def _status_totals(manifest: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "processed": 0,
        "skipped_unsupported": 0,
        "failed": 0,
        "empty": 0,
    }
    for row in manifest:
        s = row["status"]
        counts[s] = counts.get(s, 0) + 1
    return counts


def _kind_histogram(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "emails": sum(1 for r in records if r["kind"] == "email"),
        "phones": sum(1 for r in records if r["kind"] == "phone"),
    }


def _derive_run_status(
    counts: dict[str, int], unmapped: dict[str, int]
) -> str:
    if (
        counts["skipped_unsupported"]
        or counts["failed"]
        or counts["empty"]
        or any(unmapped.values())
    ):
        return "completed_with_warnings"
    return "completed"


def _sort_by_extension(
    by_extension: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """Stable sorted output for nicer diffs across runs."""
    out: dict[str, dict[str, int]] = {}
    for ext in sorted(by_extension):
        out[ext] = {k: by_extension[ext][k] for k in sorted(by_extension[ext])}
    return out


def _validation_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Compact view of the validation report for the run summary."""
    finds = {c["name"]: c["findings"] for c in report.get("checks", [])}
    return {
        "passed": report.get("passed", False),
        "raw_email_findings": finds.get(
            "no_raw_emails_in_sanitized_outputs", 0
        ),
        "raw_phone_findings": finds.get(
            "no_raw_phone_numbers_in_sanitized_outputs", 0
        ),
    }


def _write_optional_csv(
    path: Path, rows: list[dict[str, Any]], fieldnames: list[str]
) -> Path | None:
    """Write ``rows`` as CSV only if non-empty, otherwise remove a stale
    file at that path. Returns the actual path or ``None``.

    ``fieldnames`` is required so that columns are always emitted in a
    fixed order regardless of dict iteration order.
    """
    if rows:
        write_csv(path, rows, fieldnames)
        return path
    if path.exists():
        path.unlink()
    return None
