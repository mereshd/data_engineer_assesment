"""Validation tests.

Covers:
  - clean run passes
  - leaked raw email in a sanitized file is caught
  - leaked raw phone in a sanitized file is caught
  - missing input from manifest is caught
  - manifest with status=processed but no output_path is caught
"""

from __future__ import annotations

from pathlib import Path

from sanitizer import pipeline, validation


def test_clean_run_passes_validation(
    isolated_input: Path, output_root: Path, isolated_config: Path
) -> None:
    result = pipeline.run(
        input_root=isolated_input,
        output_root=output_root,
        config_path=isolated_config,
    )
    assert result.validation["passed"] is True
    for check in result.validation["checks"]:
        assert check["passed"], check


def test_leaked_email_in_output_is_detected(
    isolated_input: Path, output_root: Path, isolated_config: Path
) -> None:
    result = pipeline.run(
        input_root=isolated_input,
        output_root=output_root,
        config_path=isolated_config,
    )

    leaky = output_root / "sanitized" / "leak.txt"
    leaky.write_text("oops left in evil@example.com somehow.", encoding="utf-8")

    report = validation.run(
        input_root=isolated_input,
        output_root=output_root,
        manifest_rows=result.manifest,
    )
    findings = {c["name"]: c["findings"] for c in report["checks"]}
    assert findings["no_raw_emails_in_sanitized_outputs"] >= 1
    email_check = next(
        c for c in report["checks"]
        if c["name"] == "no_raw_emails_in_sanitized_outputs"
    )
    assert email_check["passed"] is False
    assert report["passed"] is False


def test_leaked_phone_in_output_is_detected(
    isolated_input: Path, output_root: Path, isolated_config: Path
) -> None:
    result = pipeline.run(
        input_root=isolated_input,
        output_root=output_root,
        config_path=isolated_config,
    )

    leaky = output_root / "sanitized" / "leak.txt"
    leaky.write_text("call (415) 555-1212.", encoding="utf-8")

    report = validation.run(
        input_root=isolated_input,
        output_root=output_root,
        manifest_rows=result.manifest,
    )
    findings = {c["name"]: c["findings"] for c in report["checks"]}
    assert findings["no_raw_phone_numbers_in_sanitized_outputs"] >= 1
    assert report["passed"] is False


def test_missing_input_from_manifest_is_detected(
    isolated_input: Path, output_root: Path, isolated_config: Path
) -> None:
    result = pipeline.run(
        input_root=isolated_input,
        output_root=output_root,
        config_path=isolated_config,
    )

    truncated = result.manifest[:-1]

    report = validation.run(
        input_root=isolated_input,
        output_root=output_root,
        manifest_rows=truncated,
    )
    accounted = next(
        c for c in report["checks"] if c["name"] == "all_input_files_accounted_for"
    )
    assert accounted["passed"] is False
    assert accounted["findings"] >= 1


def test_processed_without_output_is_detected(
    isolated_input: Path, output_root: Path, isolated_config: Path
) -> None:
    result = pipeline.run(
        input_root=isolated_input,
        output_root=output_root,
        config_path=isolated_config,
    )

    tampered = []
    for row in result.manifest:
        row = dict(row)
        if row["status"] == "processed":
            row["output_path"] = None
            row["output_sha256"] = None
        tampered.append(row)

    report = validation.run(
        input_root=isolated_input,
        output_root=output_root,
        manifest_rows=tampered,
    )
    check = next(
        c for c in report["checks"] if c["name"] == "processed_files_have_outputs"
    )
    assert check["passed"] is False
    assert check["findings"] >= 1
