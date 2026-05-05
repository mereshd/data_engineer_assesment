"""Post-run validation.

These checks are intentionally narrow but high-signal. They give a reviewer
four crisp yes/no answers, plus a count of findings when something fails:

  1. ``no_raw_emails_in_sanitized_outputs``   - re-scans every sanitized file
  2. ``no_raw_phone_numbers_in_sanitized_outputs`` - same, for phones
  3. ``processed_files_have_outputs``         - every ``status == processed``
                                                 manifest row has an output
                                                 file on disk + a hash
  4. ``all_input_files_accounted_for``        - every file under the input
                                                 root appears exactly once
                                                 in the manifest
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .deid import EMAIL_RE, PHONE_RE
from .utils import iter_input_files, relative_posix


SANITIZED_SUBDIR = "sanitized"


def run(
    *,
    input_root: Path,
    output_root: Path,
    manifest_rows: Iterable[dict],
) -> dict:
    """Run all four checks and return a JSON-serializable report."""
    manifest = list(manifest_rows)
    sanitized_root = Path(output_root) / SANITIZED_SUBDIR

    email_findings = _scan_outputs_for_pattern(sanitized_root, EMAIL_RE)
    phone_findings = _scan_outputs_for_pattern(sanitized_root, PHONE_RE)
    processed_missing_outputs = _check_processed_outputs(
        manifest, output_root=Path(output_root)
    )
    unaccounted = _check_all_inputs_accounted(
        manifest, input_root=Path(input_root)
    )

    checks = [
        {
            "name": "no_raw_emails_in_sanitized_outputs",
            "passed": email_findings == 0,
            "findings": email_findings,
        },
        {
            "name": "no_raw_phone_numbers_in_sanitized_outputs",
            "passed": phone_findings == 0,
            "findings": phone_findings,
        },
        {
            "name": "processed_files_have_outputs",
            "passed": processed_missing_outputs == 0,
            "findings": processed_missing_outputs,
        },
        {
            "name": "all_input_files_accounted_for",
            "passed": unaccounted == 0,
            "findings": unaccounted,
        },
    ]
    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
    }


# -------------------------------------------------------------- check #1/2


def _scan_outputs_for_pattern(sanitized_root: Path, pattern) -> int:
    """Return total match count of ``pattern`` across every sanitized file.

    Files are read with ``errors="replace"`` so a stray non-UTF-8 byte does
    not abort the validation sweep; the worst case is one extra replacement
    character in the scan buffer, which cannot itself create a false-positive
    email/phone match.
    """
    if not sanitized_root.exists():
        return 0
    total = 0
    for path in iter_input_files(sanitized_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total += sum(1 for _ in pattern.finditer(text))
    return total


# ---------------------------------------------------------------- check #3


def _check_processed_outputs(manifest: list[dict], output_root: Path) -> int:
    """Count rows with status=processed but missing output_path/hash/file."""
    bad = 0
    for row in manifest:
        if row.get("status") != "processed":
            continue
        out_rel = row.get("output_path")
        out_hash = row.get("output_sha256")
        if not out_rel or not out_hash:
            bad += 1
            continue
        if not (output_root / out_rel).is_file():
            bad += 1
    return bad


# ---------------------------------------------------------------- check #4


def _check_all_inputs_accounted(manifest: list[dict], input_root: Path) -> int:
    """Count input files missing from the manifest (or duplicated rows)."""
    discovered = {
        relative_posix(p, input_root) for p in iter_input_files(input_root)
    }
    seen: dict[str, int] = {}
    for row in manifest:
        rel = row.get("relative_path")
        if rel is None:
            continue
        seen[rel] = seen.get(rel, 0) + 1

    missing = discovered - set(seen)
    duplicates = sum(c - 1 for c in seen.values() if c > 1)
    extras = set(seen) - discovered
    return len(missing) + duplicates + len(extras)
