"""Argparse-based command-line entry point.

    python -m sanitizer --input sample_input --output output

The exit code is ``0`` on a clean ``completed`` run, ``2`` on a run that
finished but with warnings (unsupported / failed / empty files) or with
validation findings, and ``1`` on catastrophic failure (e.g. the input
folder doesn't exist, the config is unreadable).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import pipeline


DEFAULT_CONFIG = Path("config") / "entities.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sanitizer",
        description=(
            "Recursively sanitize a folder of supported text-like files "
            "(.txt / .md / .json / .csv) using a config-driven, deterministic "
            "de-identification pass. Emits a sanitized output tree plus a "
            "machine-readable run summary, per-file manifest, and validation "
            "report."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to the input folder (walked recursively).",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path to the output folder. Will contain sanitized/ and reports/.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to entities config JSON (default: {DEFAULT_CONFIG}).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable INFO-level logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # The default log format is intentionally compact ("LEVEL message")
    # because this is an interactive CLI, not a server log. With --verbose
    # the output is one line per file outcome plus run-boundary events,
    # which reads cleanly without timestamps and module names cluttering
    # every line. (A production deployment that pipes logs to an
    # aggregator would reconfigure this to include timestamps + structured
    # fields; that's a one-line change here.)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(message)s",
    )

    try:
        result = pipeline.run(
            input_root=args.input,
            output_root=args.output,
            config_path=args.config,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - top-level safety net
        print(f"error: pipeline crashed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    s = result.summary
    v_passed = "PASSED" if result.validation.get("passed") else "FAILED"
    unmapped_total = (
        s["unmapped"]["emails"] + s["unmapped"]["phones"]
    )
    # The first four counters tally *files* (matches the manifest's
    # status histogram). "unmapped" is different - it tallies
    # per-occurrence records routed to pii_quarantine.csv - so we
    # spell out the unit to keep the line unambiguous.
    print(
        f"Run {result.run_id}: {s['files_processed']} processed / "
        f"{s['files_skipped_unsupported']} skipped / "
        f"{s['files_failed']} failed / "
        f"{s['empty_files']} empty / "
        f"{unmapped_total} unmapped records - validation: {v_passed}"
    )
    print(f"  summary:             {result.summary_path}")
    print(f"  manifest:            {result.manifest_path}")
    print(f"  validation:          {result.validation_path}")
    if result.pii_transformations_path is not None:
        print(
            f"  pii_transformations: {result.pii_transformations_path} "
            f"({len(result.pii_transformations)} records)"
        )
    if result.pii_quarantine_path is not None:
        print(
            f"  pii_quarantine:      {result.pii_quarantine_path} "
            f"({len(result.pii_quarantine)} record"
            f"{'s' if len(result.pii_quarantine) != 1 else ''})"
        )
    print(f"  analytics:           {result.analytics_path}")

    if result.run_status != "completed" or not result.validation.get("passed"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
