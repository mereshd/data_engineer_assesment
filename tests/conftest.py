"""Shared pytest fixtures."""

from __future__ import annotations

import csv
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def sample_config_path() -> Path:
    return PROJECT_ROOT / "config" / "entities.json"


@pytest.fixture
def sample_input_path() -> Path:
    return PROJECT_ROOT / "sample_input"


@pytest.fixture
def isolated_input(tmp_path: Path, sample_input_path: Path) -> Path:
    """Copy sample_input/ into a tmp directory so tests can mutate freely."""
    dest = tmp_path / "input"
    shutil.copytree(sample_input_path, dest)
    return dest


@pytest.fixture
def isolated_config(tmp_path: Path, sample_config_path: Path) -> Path:
    dest = tmp_path / "entities.json"
    dest.write_text(
        sample_config_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return dest


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    return tmp_path / "output"


@dataclass
class DemoRun:
    """Bundle of artifacts from a single pipeline run on the sample input.

    Lets pipeline tests share one full run instead of paying the
    (small but multiplied) cost of running the pipeline per test, and
    keeps test bodies focused on assertions rather than setup
    boilerplate.
    """

    result: Any
    output_root: Path
    manifest: list[dict]
    transformations: list[dict]
    quarantine: list[dict]
    analytics_html: str


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="session")
def demo_run(tmp_path_factory: pytest.TempPathFactory) -> DemoRun:
    """Run the pipeline once per session against the bundled sample input.

    Most pipeline tests are read-only assertions on these artifacts; the
    handful of tests that need a fresh run (idempotency, empty-file
    handling, clean-input handling, validation tampering) keep using
    their own ``isolated_input`` / ``tmp_path`` instead.
    """
    from sanitizer import pipeline

    output_root = tmp_path_factory.mktemp("demo_output")
    result = pipeline.run(
        input_root=PROJECT_ROOT / "sample_input",
        output_root=output_root,
        config_path=PROJECT_ROOT / "config" / "entities.json",
    )
    analytics_path = output_root / "reports" / "analytics.html"
    return DemoRun(
        result=result,
        output_root=output_root,
        manifest=_read_jsonl(output_root / "reports" / "file_manifest.jsonl"),
        transformations=_read_csv(
            output_root / "reports" / "pii_transformations.csv"
        ),
        quarantine=_read_csv(
            output_root / "reports" / "pii_quarantine.csv"
        ),
        analytics_html=(
            analytics_path.read_text(encoding="utf-8")
            if analytics_path.exists() else ""
        ),
    )


@pytest.fixture
def minimal_config_path(tmp_path: Path) -> Path:
    """Build a tiny config for fast unit tests of the de-identifier."""
    cfg = {
        "persons": [
            {
                "canonical_id": "PERSON_001",
                "aliases": ["John Miller", "John"],
                "emails": [{"value": "john@acme.com", "token": "EMAIL_001"}],
                "phones": [],
            },
            {
                "canonical_id": "PERSON_002",
                "aliases": ["Sarah Chen", "Sarah"],
                "emails": [
                    {"value": "sarah@betahealth.io", "token": "EMAIL_002"}
                ],
                "phones": [
                    {"value": "+1-212-555-0199", "token": "PHONE_001"}
                ],
            },
        ],
        "organizations": [
            {"canonical_id": "ORG_001", "aliases": ["BetaHealth"]},
            {"canonical_id": "ORG_002", "aliases": ["Acme Inc.", "Acme"]},
        ],
    }
    path = tmp_path / "minimal_entities.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path
