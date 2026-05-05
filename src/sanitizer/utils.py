"""Small helpers shared across the pipeline.

Kept dependency-free on purpose: stdlib-only is one of the v1 promises.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Iterator

_HASH_CHUNK = 1 << 20  # 1 MiB


def sha256_bytes(data: bytes) -> str:
    """Return hex SHA-256 of an in-memory bytes object."""
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    """Return hex SHA-256 of a file streamed in fixed-size chunks.

    Streaming keeps memory bounded for the (rare) large input we might see.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def iter_input_files(root: Path) -> Iterator[Path]:
    """Yield every file under ``root`` in a deterministic, sorted order.

    Sorted by directory then filename so the run order is reproducible across
    OSes (Windows / macOS / Linux walk in different default orders) and across
    repeated runs. This is what makes idempotency tests sane.
    """
    root = Path(root)
    if not root.exists():
        return
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name


def relative_posix(path: Path, root: Path) -> str:
    """Return the path of ``path`` relative to ``root`` using POSIX separators.

    Posix-style relative paths are what we want in manifests so the artifacts
    are portable between Windows and Unix readers.
    """
    rel = Path(path).resolve().relative_to(Path(root).resolve())
    return rel.as_posix()


def ensure_parent(path: Path) -> None:
    """Create the parent directory of ``path`` (idempotent)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def utc_now() -> _dt.datetime:
    """Return current UTC datetime (timezone-aware)."""
    return _dt.datetime.now(_dt.timezone.utc)


def utc_now_iso() -> str:
    """Return current UTC time as ``YYYY-MM-DDTHH:MM:SSZ`` (no microseconds)."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def make_run_id(now: _dt.datetime | None = None) -> str:
    """Build the canonical run identifier ``YYYYMMDD_HHMMSS`` in UTC."""
    now = now or utc_now()
    return now.strftime("%Y%m%d_%H%M%S")


def write_json(path: Path, obj: Any) -> None:
    """Write ``obj`` to ``path`` as pretty JSON, creating parents as needed."""
    ensure_parent(path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")


def write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    """Write an iterable of records to ``path`` as JSON Lines."""
    ensure_parent(path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


def write_csv(
    path: Path, rows: Iterable[dict], fieldnames: list[str]
) -> None:
    """Write an iterable of dict rows to ``path`` as RFC-4180 CSV.

    Uses ``QUOTE_MINIMAL`` so cells with commas, double-quotes, or angle
    brackets get quoted automatically; everything else stays unquoted. The
    line terminator is ``\\n`` so the file is identical across runs and
    across operating systems.
    """
    ensure_parent(path)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=fieldnames,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def empty_replacement_counts() -> dict[str, int]:
    """The canonical zeroed counts dict shape used everywhere."""
    return {"emails": 0, "phones": 0, "persons": 0, "organizations": 0}


def add_counts(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Add two ``empty_replacement_counts``-shaped dicts in place into ``a``."""
    for k, v in b.items():
        a[k] = a.get(k, 0) + v
    return a
