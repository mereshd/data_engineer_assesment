"""Pipeline integration tests.

Most assertions read from the session-scoped :func:`demo_run` fixture,
which runs the pipeline once per session against the bundled
``sample_input/``. Tests that need a fresh, controlled run (idempotency,
empty-file handling, clean-input handling) build their own input tree
under ``tmp_path``.

Covers:
  - file classification: processed / skipped_unsupported / failed / empty
  - per-extension status histogram
  - run-summary totals consistent with the manifest
  - sanitized outputs contain no raw PII (placeholders for unmapped)
  - byte-identical sanitized files across repeated runs
  - mapped PII -> pii_transformations.csv, unmapped -> pii_quarantine.csv
  - PII record schema, location format per processor, snippet privacy
  - cross-document dedup via value_hash
  - CSV format: stable header, RFC 4180 quoting round-trips cleanly
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sanitizer import pipeline


# Tests that only need to read demo artifacts use the ``demo_run`` fixture
# from conftest. The local helpers below are for tests that build a
# bespoke input tree.


def _run(input_root: Path, output_root: Path, config_path: Path):
    return pipeline.run(
        input_root=input_root,
        output_root=output_root,
        config_path=config_path,
    )


def _read_manifest_jsonl(output_root: Path) -> list[dict]:
    path = output_root / "reports" / "file_manifest.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------- top-level


def test_full_run_completes_with_warnings_on_sample_input(demo_run) -> None:
    """Smoke test: every report exists, file counts match the seeded
    sample, run_status is ``completed_with_warnings`` (because we have
    unsupported / failed files AND seeded unmapped values)."""
    out = demo_run.output_root
    assert (out / "sanitized").is_dir()
    for report in (
        "run_summary.json",
        "file_manifest.jsonl",
        "validation_report.json",
        "pii_transformations.csv",
        "pii_quarantine.csv",
    ):
        assert (out / "reports" / report).is_file(), report

    s = demo_run.result.summary
    assert s["run_status"] == "completed_with_warnings"
    assert s["files_discovered"] == 10
    assert s["files_processed"] == 5
    assert s["files_skipped_unsupported"] == 4
    assert s["files_failed"] == 1
    assert s["empty_files"] == 0
    # Unmapped findings alone are also enough to trigger the warning state.
    assert any(s["unmapped"].values())


def test_unsupported_files_appear_in_manifest(demo_run) -> None:
    """Each unsupported file lives in its own format-named directory and
    appears in the manifest with ``status=skipped_unsupported`` plus no
    output path / hash / error."""
    unsupported = [
        r for r in demo_run.manifest if r["status"] == "skipped_unsupported"
    ]
    rels = {r["relative_path"] for r in unsupported}
    assert rels == {
        "contracts/contract.pdf",
        "screenshots/screenshot.png",
        "spreadsheets/model_export.xlsx",
        "archives/archive.zip",
    }
    for row in unsupported:
        assert row["output_path"] is None
        assert row["output_sha256"] is None
        assert row["error"] is None


def test_malformed_json_is_failed_but_run_continues(demo_run) -> None:
    """The malformed thread is the *only* failure; the well-formed thread
    in the same directory still gets processed."""
    failed = [r for r in demo_run.manifest if r["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["relative_path"] == "slack/malformed_thread.json"
    assert failed[0]["output_path"] is None
    assert "JSONDecodeError" in (failed[0]["error"] or "")

    processed = {r["relative_path"] for r in demo_run.manifest if r["status"] == "processed"}
    assert "slack/general/thread_001.json" in processed


def test_processor_dispatch_handles_all_supported_types(demo_run) -> None:
    """Each supported extension routes through its processor with the
    correct ``records_processed`` accounting (rows for CSV, list-len for
    JSON-array roots, 1 for object-root JSON / TXT / MD)."""
    by_path = {r["relative_path"]: r for r in demo_run.manifest}

    expectations = {
        "email/inbox.csv":               (".csv", 4),  # 4 data rows
        "docs/onboarding_notes.md":      (".md", 1),
        "docs/customer_notes.txt":       (".txt", 1),
        "slack/general/thread_001.json": (".json", 2),  # array of 2 messages
        "jira/issues.json":              (".json", 1),  # object root
    }
    for rel, (ext, records) in expectations.items():
        row = by_path[rel]
        assert row["status"] == "processed", rel
        assert row["extension"] == ext, rel
        assert row["records_processed"] == records, rel


def test_by_extension_counts_have_expected_shape(demo_run) -> None:
    by_ext = demo_run.result.summary["by_extension"]
    assert by_ext[".json"] == {"failed": 1, "processed": 2}
    assert by_ext[".csv"] == {"processed": 1}
    assert by_ext[".md"] == {"processed": 1}
    assert by_ext[".txt"] == {"processed": 1}
    for ext in (".pdf", ".png", ".xlsx", ".zip"):
        assert by_ext[ext] == {"skipped_unsupported": 1}


def test_run_summary_totals_match_manifest(demo_run) -> None:
    """The summary's per-status counts and aggregate ``replacements``
    are computed *from* the manifest; any drift here means the writer
    or the aggregator has a bug."""
    s = demo_run.result.summary
    manifest = demo_run.manifest

    by_status = {
        "processed": "files_processed",
        "skipped_unsupported": "files_skipped_unsupported",
        "failed": "files_failed",
        "empty": "empty_files",
    }
    for status, key in by_status.items():
        assert s[key] == sum(1 for r in manifest if r["status"] == status), key

    aggregated = {"emails": 0, "phones": 0, "persons": 0, "organizations": 0}
    for row in manifest:
        for k, v in row["replacements"].items():
            aggregated[k] += v
    assert s["replacements"] == aggregated


def test_sanitized_outputs_contain_no_raw_pii_and_use_placeholders(
    demo_run,
) -> None:
    """Validation passes (regex sweep finds no raw email/phone in the
    sanitized tree), and the seeded markdown specifically shows the
    ``<UNMAPPED_*>`` placeholders for the unmapped vendor values."""
    assert demo_run.result.validation["passed"] is True
    findings = {c["name"]: c["findings"] for c in demo_run.result.validation["checks"]}
    assert findings["no_raw_emails_in_sanitized_outputs"] == 0
    assert findings["no_raw_phone_numbers_in_sanitized_outputs"] == 0

    sanitized_md = (
        demo_run.output_root / "sanitized" / "docs" / "onboarding_notes.md"
    ).read_text(encoding="utf-8")
    assert "<UNMAPPED_EMAIL>" in sanitized_md
    assert "<UNMAPPED_PHONE>" in sanitized_md
    assert "vendor.support@externalpartner.com" not in sanitized_md
    assert "415-555-0142" not in sanitized_md


# --------------------------------------------------------- pii row reports


@pytest.fixture
def pii_reports(demo_run):
    """Just the two PII row-level reports, parsed from CSV."""
    return {
        "mapped": demo_run.transformations,
        "unmapped": demo_run.quarantine,
    }


def test_pii_reports_files_match_in_memory_records(demo_run) -> None:
    """The CSV parsed back from disk matches what the pipeline returned
    in memory (and the result paths point at the right files)."""
    out = demo_run.output_root
    result = demo_run.result

    assert result.pii_transformations_path == out / "reports" / "pii_transformations.csv"
    assert result.pii_quarantine_path == out / "reports" / "pii_quarantine.csv"
    assert result.pii_transformations == demo_run.transformations
    assert result.pii_quarantine == demo_run.quarantine
    assert len(demo_run.transformations) > 0
    assert len(demo_run.quarantine) >= 4  # 1 email + 1 phone in 2 files


PII_FIELDS = {
    "file", "kind", "value", "value_hash", "token", "status",
    "location", "snippet",
}


@pytest.mark.parametrize(
    "status, allowed_kinds, allowed_tokens",
    [
        ("mapped",   {"email", "phone", "person", "organization"}, None),
        ("unmapped", {"email", "phone"},                           {"<UNMAPPED_EMAIL>", "<UNMAPPED_PHONE>"}),
    ],
)
def test_pii_records_have_required_fields(
    pii_reports, status, allowed_kinds, allowed_tokens
) -> None:
    """Both reports share the same 8-field schema; only ``status`` and
    the set of valid ``kind`` / ``token`` values differ."""
    for record in pii_reports[status]:
        assert set(record) == PII_FIELDS, record
        assert record["status"] == status
        assert record["kind"] in allowed_kinds, record
        assert len(record["value_hash"]) == 8
        assert record["snippet"]
        if allowed_tokens is not None:
            assert record["token"] in allowed_tokens, record
        else:
            assert record["token"]
        # For email/phone the raw normalized value must not appear in its
        # own snippet (snippet is rendered against fully sanitized text).
        if record["kind"] in {"email", "phone"}:
            assert record["value"] not in record["snippet"], record


def test_pii_locations_use_format_appropriate_to_processor(demo_run) -> None:
    """Same record schema across processors, but the ``location`` string
    format adapts:

      - text/markdown (.txt/.md): ``line N, column M``
      - CSV:                       ``row N, column "X"``
      - JSON:                      ``$.path[i].field``
    """
    all_records = demo_run.transformations + demo_run.quarantine

    by_ext: dict[str, list[dict]] = {}
    for r in all_records:
        ext = "." + r["file"].rsplit(".", 1)[-1]
        by_ext.setdefault(ext, []).append(r)

    assert by_ext.get(".md") and by_ext.get(".csv") and by_ext.get(".json")
    for r in by_ext.get(".md", []) + by_ext.get(".txt", []):
        assert r["location"].startswith("line "), r
    for r in by_ext.get(".csv", []):
        assert r["location"].startswith("row ") and 'column "' in r["location"], r
    for r in by_ext.get(".json", []):
        assert r["location"].startswith("$"), r


def test_summary_totals_consistent_with_pii_reports(demo_run) -> None:
    """Single source-of-truth check: row counts in each PII report sum
    to the matching ``summary.replacements`` / ``summary.unmapped``
    totals, and per-file counts in the manifest agree too."""
    s = demo_run.result.summary
    kind_to_replacement_key = {
        "email": "emails", "phone": "phones",
        "person": "persons", "organization": "organizations",
    }

    # transformations -> summary.replacements
    expected = {v: 0 for v in kind_to_replacement_key.values()}
    for r in demo_run.transformations:
        expected[kind_to_replacement_key[r["kind"]]] += 1
    assert expected == s["replacements"]

    # quarantine -> summary.unmapped
    expected_unmapped = {
        "emails": sum(1 for r in demo_run.quarantine if r["kind"] == "email"),
        "phones": sum(1 for r in demo_run.quarantine if r["kind"] == "phone"),
    }
    assert expected_unmapped == s["unmapped"]

    # quarantine -> per-file unmapped on the manifest
    per_file = {"emails": 0, "phones": 0}
    for row in demo_run.manifest:
        per_file["emails"] += row["unmapped"]["emails"]
        per_file["phones"] += row["unmapped"]["phones"]
    assert per_file == expected_unmapped


def test_pii_transformations_cover_all_four_kinds(demo_run) -> None:
    """The seeded sample exercises every kind - if one's missing,
    something silently dropped findings between scan and write."""
    kinds = {r["kind"] for r in demo_run.transformations}
    assert kinds == {"email", "phone", "person", "organization"}


def test_same_unmapped_value_has_same_hash_across_files(demo_run) -> None:
    """The seeded vendor email + phone appear in both the markdown and
    the CSV; ``value_hash`` must be identical for the same value
    regardless of which file it was found in."""
    by_value: dict[str, list[dict]] = {}
    for r in demo_run.quarantine:
        by_value.setdefault(r["value"], []).append(r)
    multi_doc = {v: rs for v, rs in by_value.items() if len(rs) > 1}
    assert multi_doc, "expected at least one value to recur across files"

    for occurrences in multi_doc.values():
        assert len({r["value_hash"] for r in occurrences}) == 1
        assert len({r["file"] for r in occurrences}) > 1


def test_pii_csvs_have_stable_format_and_round_trip_cleanly(demo_run) -> None:
    """Both CSVs emit the same header in the same order, RFC-4180
    quoting handles embedded commas and double-quotes (locations like
    ``row 4, column "from"``), and parsing the file recovers the exact
    same value the in-memory record had."""
    expected_header = "file,kind,value,value_hash,token,status,location,snippet"
    for filename in ("pii_transformations.csv", "pii_quarantine.csv"):
        path = demo_run.output_root / "reports" / filename
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == expected_header, filename

    in_memory = {
        (r["file"], r["location"], r["value"], r["status"]): r
        for r in (
            demo_run.result.pii_transformations
            + demo_run.result.pii_quarantine
        )
    }
    parsed = {
        (r["file"], r["location"], r["value"], r["status"]): r
        for r in demo_run.transformations + demo_run.quarantine
    }
    assert set(in_memory) == set(parsed)
    for key, row in parsed.items():
        assert row == in_memory[key], key

    # Both kinds of CSV-tricky chars actually show up in the output.
    all_records = demo_run.transformations + demo_run.quarantine
    assert any("," in r["location"] for r in all_records)
    assert any('"' in r["location"] for r in all_records)


# --------------------------------------------- analytics dashboard


def test_analytics_html_is_written(demo_run) -> None:
    """Every run produces a self-contained analytics.html, alongside
    the other reports. RunResult.analytics_path points at it; the
    file lives under <output>/reports/."""
    expected = demo_run.output_root / "reports" / "analytics.html"
    assert expected.is_file()
    assert demo_run.result.analytics_path == expected
    assert len(demo_run.analytics_html) > 0


def test_analytics_html_embeds_run_summary_and_graph_data(demo_run) -> None:
    """Pull the JSON payload back out of the embedded
    <script type="application/json"> block and verify it carries the
    summary, graph nodes/edges, and quarantine groupings we expect."""
    import json
    import re

    match = re.search(
        r'<script type="application/json" id="dashboard-data">\s*(.+?)\s*</script>',
        demo_run.analytics_html,
        re.DOTALL,
    )
    assert match, "embedded dashboard-data block not found"

    # The Python side replaces "<" with "\u003c" inside the JSON to
    # prevent any string in the data from breaking the surrounding
    # HTML; JSON.parse handles that natively, and so does json.loads.
    data = json.loads(match.group(1))

    # Summary embeds the run id and totals.
    assert data["summary"]["run_id"] == demo_run.result.run_id
    assert data["summary"]["files_processed"] == 5
    assert data["summary"]["unmapped"] == {"emails": 2, "phones": 2}

    # Graph has one node per processed file plus one per distinct
    # canonical entity, and at least one edge.
    node_groups = {n["group"] for n in data["graph"]["nodes"]}
    assert "file" in node_groups
    assert {"person", "organization", "email", "phone"} <= node_groups
    assert len(data["graph"]["edges"]) > 0

    # Quarantine grouped by (kind, value, hash) - the seeded vendor
    # email + phone each have 2 occurrences (markdown + CSV).
    by_value = {q["value"]: q for q in data["quarantine"]}
    assert "vendor.support@externalpartner.com" in by_value
    assert by_value["vendor.support@externalpartner.com"]["count"] == 2


def test_analytics_html_does_not_leak_raw_pii_via_neighboring_snippets(
    demo_run,
) -> None:
    """Snippets in the embedded payload come from the fully sanitized
    text, so the same privacy contract that holds for the CSV
    snippets holds here."""
    import json
    import re

    match = re.search(
        r'<script type="application/json" id="dashboard-data">\s*(.+?)\s*</script>',
        demo_run.analytics_html,
        re.DOTALL,
    )
    data = json.loads(match.group(1))

    for q in data["quarantine"]:
        for occ in q["occurrences"]:
            # The raw value should never appear verbatim in its own snippet.
            assert q["value"] not in occ["snippet"], (q["value"], occ)


def test_analytics_html_links_back_to_other_reports(demo_run) -> None:
    """The header links to the machine-readable artifacts so a reviewer
    can pivot from the dashboard to the underlying data."""
    for filename in (
        "run_summary.json",
        "file_manifest.jsonl",
        "validation_report.json",
        "pii_transformations.csv",
        "pii_quarantine.csv",
    ):
        assert (
            f'href="{filename}"' in demo_run.analytics_html
        ), filename


# --------------------------------------------- bespoke-input scenarios


def test_empty_file_is_recorded_as_empty(
    tmp_path: Path, isolated_config: Path
) -> None:
    """The bundled sample has no empty file (we don't ship a 0-byte
    "demo" input), but the empty-file path still has a contract:
    0-byte input -> ``status=empty`` + 0-byte mirrored output."""
    input_root = tmp_path / "tiny_input"
    (input_root / "blank").mkdir(parents=True)
    (input_root / "blank" / "blank.txt").write_bytes(b"")
    output_root = tmp_path / "tiny_output"

    _run(input_root, output_root, isolated_config)
    manifest = _read_manifest_jsonl(output_root)

    empties = [r for r in manifest if r["status"] == "empty"]
    assert len(empties) == 1
    row = empties[0]
    assert row["relative_path"] == "blank/blank.txt"
    assert row["output_path"] == "sanitized/blank/blank.txt"
    out_file = output_root / "sanitized" / "blank" / "blank.txt"
    assert out_file.is_file()
    assert out_file.read_bytes() == b""
    assert row["input_sha256"] == row["output_sha256"]


def test_running_twice_produces_byte_identical_sanitized_files(
    isolated_input: Path, tmp_path: Path, isolated_config: Path
) -> None:
    """Sorted traversal + deterministic placeholders + token-stable
    config = byte-identical sanitized outputs on repeat runs."""
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    _run(isolated_input, out_a, isolated_config)
    _run(isolated_input, out_b, isolated_config)

    a_files = sorted(p for p in (out_a / "sanitized").rglob("*") if p.is_file())
    b_files = sorted(p for p in (out_b / "sanitized").rglob("*") if p.is_file())
    assert [p.relative_to(out_a / "sanitized") for p in a_files] == [
        p.relative_to(out_b / "sanitized") for p in b_files
    ]
    assert a_files
    for a, b in zip(a_files, b_files):
        assert a.read_bytes() == b.read_bytes(), f"diverged: {a.name}"


def test_no_pii_quarantine_file_when_run_has_no_unmapped(
    tmp_path: Path, isolated_config: Path
) -> None:
    """A clean input with only mapped values produces a transformations
    CSV but **not** a quarantine CSV - we don't ship empty files just
    to look busy."""
    input_root = tmp_path / "clean_input"
    (input_root / "docs").mkdir(parents=True)
    (input_root / "docs" / "note.md").write_text(
        "Sarah Chen at sarah@betahealth.io was on the call.\n",
        encoding="utf-8",
    )
    output_root = tmp_path / "clean_output"

    result = _run(input_root, output_root, isolated_config)
    qpath = output_root / "reports" / "pii_quarantine.csv"
    tpath = output_root / "reports" / "pii_transformations.csv"

    assert not qpath.exists()
    assert result.pii_quarantine_path is None
    assert result.pii_quarantine == []
    assert result.summary["unmapped"] == {"emails": 0, "phones": 0}

    assert tpath.is_file()
    assert result.pii_transformations_path == tpath
    assert len(result.pii_transformations) >= 2  # email + person at minimum
