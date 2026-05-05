"""Per-file-type processors.

Each processor reads a single input file and returns:

    (output_bytes, records_processed, replacement_counts, finding_records)

The pipeline catches exceptions raised by processors and records the file
as ``failed``; processors do not need to do their own error logging.

A *finding record* is a dict with keys ``kind``, ``value``, ``value_hash``,
``token``, ``status``, ``location``, and ``snippet`` - one per regex match
the de-identifier acted on (mapped *or* unmapped), annotated with a
structural ``location`` the processor knows about (line + column for text,
JSON path for JSON, row + column for CSV). The pipeline adds the file path
and routes the record by ``status``: ``"mapped"`` to
``pii_transformations.csv``, ``"unmapped"`` to ``pii_quarantine.csv``.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .deid import DeIdentifier, Finding
from .utils import add_counts, empty_replacement_counts


FindingRecord = dict


def process_txt_md(
    path: Path, deid: DeIdentifier
) -> tuple[bytes, int, dict[str, int], list[FindingRecord]]:
    """Sanitize a plain-text or Markdown file.

    Findings get a 1-indexed ``line N, column M`` location derived from the
    char offset returned by the de-identifier.
    """
    raw = Path(path).read_text(encoding="utf-8")
    sanitized, counts, findings = deid.apply(raw)

    records = [
        _finding_to_record(f, _line_column_location(raw, f.start_offset))
        for f in findings
    ]
    return sanitized.encode("utf-8"), 1, counts, records


def process_json(
    path: Path, deid: DeIdentifier
) -> tuple[bytes, int, dict[str, int], list[FindingRecord]]:
    """Sanitize a JSON file, preserving structure.

    Only string values are sanitized. Keys, numbers, booleans, and ``null``
    pass through untouched. Output is pretty-printed with ``indent=2`` and
    ``ensure_ascii=False``. ``records_processed`` is ``len(top)`` for a
    top-level array, otherwise ``1``.

    Findings get a JSON-path location like ``$.issues[0].comments[1].body``
    so a reviewer can jump to the exact field that contained the value.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    sanitized, counts, findings = sanitize_json_value(data, deid)
    record_count = len(sanitized) if isinstance(sanitized, list) else 1

    records = [_finding_to_record(f, f.location_hint) for f in findings]
    payload = json.dumps(sanitized, indent=2, ensure_ascii=False) + "\n"
    return payload.encode("utf-8"), record_count, counts, records


def process_csv(
    path: Path, deid: DeIdentifier
) -> tuple[bytes, int, dict[str, int], list[FindingRecord]]:
    """Sanitize a CSV file, preserving headers and basic quoting.

    Findings get a ``row N, column "X"`` location, where ``N`` is the
    1-indexed data row (the header is row 0 - which we never sanitize).
    """
    counts = empty_replacement_counts()
    records: list[FindingRecord] = []

    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return b"", 0, counts, records

        out_buf = io.StringIO(newline="")
        writer = csv.DictWriter(
            out_buf,
            fieldnames=list(reader.fieldnames),
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        writer.writeheader()

        record_count = 0
        for row in reader:
            record_count += 1
            sanitized_row: dict[str, Any] = {}
            for column, value in row.items():
                if isinstance(value, str):
                    new_value, c, findings = deid.apply(value)
                    add_counts(counts, c)
                    location = f'row {record_count}, column "{column}"'
                    records.extend(
                        _finding_to_record(f, location) for f in findings
                    )
                    sanitized_row[column] = new_value
                else:
                    sanitized_row[column] = value
            writer.writerow(sanitized_row)

        return out_buf.getvalue().encode("utf-8"), record_count, counts, records


# ------------------------------------------------------------- json helpers


class _LocatedFinding:
    """A :class:`Finding` augmented with the JSON path it was found at.

    Used internally by :func:`sanitize_json_value` so the recursive walker
    can produce the JSON-path location while the finding is still close to
    the recursion frame that knows the path.
    """

    __slots__ = ("kind", "value", "value_hash", "token", "status",
                 "snippet", "location_hint", "start_offset", "end_offset")

    def __init__(self, finding: Finding, path: str) -> None:
        self.kind = finding.kind
        self.value = finding.value
        self.value_hash = finding.value_hash
        self.token = finding.token
        self.status = finding.status
        self.snippet = finding.snippet
        self.start_offset = finding.start_offset
        self.end_offset = finding.end_offset
        self.location_hint = path


def sanitize_json_value(
    value: Any, deid: DeIdentifier
) -> tuple[Any, dict[str, int], list[_LocatedFinding]]:
    """Recursively sanitize a JSON-decoded value.

    Returns ``(sanitized_value, counts, located_findings)``. Each finding's
    ``location_hint`` is a JSON path string (e.g. ``$.issues[0].body``)
    pointing at the exact string in which the unmapped value was found.
    """
    counts = empty_replacement_counts()
    located: list[_LocatedFinding] = []
    sanitized = _walk(value, deid, counts, located, path="$")
    return sanitized, counts, located


def _walk(
    value: Any,
    deid: DeIdentifier,
    counts: dict[str, int],
    located: list[_LocatedFinding],
    path: str,
) -> Any:
    if isinstance(value, str):
        new_value, c, findings = deid.apply(value)
        add_counts(counts, c)
        for f in findings:
            located.append(_LocatedFinding(f, path))
        return new_value
    if isinstance(value, list):
        return [
            _walk(item, deid, counts, located, f"{path}[{i}]")
            for i, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: _walk(item, deid, counts, located, _join_path(path, key))
            for key, item in value.items()
        }
    return value


def _join_path(parent: str, key: str) -> str:
    """Attach ``key`` to a JSON path ``parent``, choosing dotted vs bracket
    notation based on whether the key is a clean identifier."""
    if key.isidentifier():
        return f"{parent}.{key}"
    safe = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'{parent}["{safe}"]'


# ----------------------------------------------------------- finding output


def _finding_to_record(f, location: str) -> FindingRecord:
    """Convert a :class:`Finding` (or :class:`_LocatedFinding`) to a flat
    record dict suitable for ``pii_transformations.csv`` /
    ``pii_quarantine.csv``.

    Drops raw character offsets (only useful inside the de-identifier
    itself) and surfaces the structural ``location`` the caller computed.
    """
    return {
        "kind": f.kind,
        "value": f.value,
        "value_hash": f.value_hash,
        "token": f.token,
        "status": f.status,
        "location": location,
        "snippet": f.snippet,
    }


def _line_column_location(text: str, offset: int) -> str:
    """Translate a character offset into a 1-indexed ``line N, column M``
    location suitable for plain-text findings."""
    line = text.count("\n", 0, offset) + 1
    last_newline = text.rfind("\n", 0, offset)
    column = offset - last_newline if last_newline >= 0 else offset + 1
    return f"line {line}, column {column}"
