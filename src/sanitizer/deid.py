"""Deterministic, config-driven de-identification.

Replacement order matters and is hard-coded (per the v1 spec):

    1. emails       - so domains aren't corrupted by org alias passes
    2. phones       - so digit-runs aren't eaten by other regexes
    3. persons      - aliases applied longest-first so "Sarah Chen" wins
                      over "Sarah" when both could match
    4. organizations - aliases applied longest-first; lookarounds (not \\b)
                      are used because aliases like "Acme Inc." end in '.'

For each PII match we emit a :class:`Finding` carrying the raw value, the
token / placeholder it was replaced with, and a ``status`` of ``"mapped"``
(configured in entities.json) or ``"unmapped"`` (regex matched but no
config entry). The pipeline writes mapped findings to
``pii_transformations.csv`` and unmapped findings to
``pii_quarantine.csv`` - same schema, different downstream handling.

Internally :py:meth:`DeIdentifier.apply` runs in three phases so that
(a) ``Finding.start_offset`` is stable in the *original* input text - good
for line numbers in plain text - and (b) ``Finding.snippet`` is rendered
from the *fully sanitized* text - so the snippet itself contains no raw
PII even when neighbouring matches haven't been triaged yet.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .utils import empty_replacement_counts


# A reasonably permissive but well-bounded email regex. Word boundaries on
# both sides keep us out of token tails like ``EMAIL_001`` or random words.
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Phones we recognize:
#   +1-212-555-0199        (international with separators)
#   1-212-555-0199         (country code without leading +)
#   (212) 555-0199         (US parens form)
#   212-555-0199 / 212.555.0199 / 212 555 0199
# The (?<!\w)/(?!\w) anchors keep us from chewing into ISO dates like
# 2026-05-01: those would require an extra digit immediately adjacent.
PHONE_RE = re.compile(
    r"""
    (?<!\w)
    (?:\+?\d{1,3}[-.\s])?       # optional country code with separator
    \(?\d{3}\)?[-.\s]?          # area code, optionally parenthesized
    \d{3}[-.\s]?                # exchange
    \d{4}                       # subscriber
    (?!\w)
    """,
    re.VERBOSE,
)


UNMAPPED_EMAIL = "<UNMAPPED_EMAIL>"
UNMAPPED_PHONE = "<UNMAPPED_PHONE>"

# Window (in characters) of context to capture on each side of a finding.
SNIPPET_WINDOW = 60

# Status values for Finding.
STATUS_MAPPED = "mapped"
STATUS_UNMAPPED = "unmapped"


@dataclass(frozen=True)
class _AliasRule:
    """A single compiled alias replacement rule."""

    alias: str
    token: str
    pattern: re.Pattern[str]


@dataclass
class Finding:
    """One PII detection inside a single string.

    Captures both successful replacements (configured value -> known token)
    and unmapped values (regex match -> placeholder token, awaiting operator
    review). The pipeline routes by ``status``: ``"mapped"`` rows go to
    ``pii_transformations.csv``, ``"unmapped"`` rows go to
    ``pii_quarantine.csv``.
    """

    kind: str           # "email" | "phone" | "person" | "organization"
    value: str          # raw text matched in the input (lowercased for
                        #   emails, digits-only for phones, verbatim for
                        #   persons / organizations)
    value_hash: str     # first 8 chars of sha256(value); cross-doc dedup key
    token: str          # what we replaced this match with - either the
                        #   configured token (EMAIL_001, PERSON_002, ...) or
                        #   the unmapped placeholder
    status: str         # STATUS_MAPPED | STATUS_UNMAPPED
    snippet: str        # +/- SNIPPET_WINDOW chars of context, rendered
                        #   against the fully sanitized text (no raw PII)
    start_offset: int   # char offset of the match in the original input
    end_offset: int     # exclusive

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "value": self.value,
            "value_hash": self.value_hash,
            "token": self.token,
            "status": self.status,
            "snippet": self.snippet,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
        }


@dataclass
class DeIdentifier:
    """Apply deterministic replacements to a single string.

    Build via :py:meth:`from_config_path` or :py:meth:`from_config_dict`.
    Use :py:meth:`apply` to sanitize a string and get back replacement
    counts plus per-occurrence findings (mapped + unmapped).
    """

    emails: dict[str, str]
    phones: dict[str, str]
    person_rules: list[_AliasRule] = field(default_factory=list)
    org_rules: list[_AliasRule] = field(default_factory=list)

    @classmethod
    def from_config_path(cls, config_path: Path) -> "DeIdentifier":
        config_path = Path(config_path)
        with open(config_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_config_dict(data)

    @classmethod
    def from_config_dict(cls, data: dict) -> "DeIdentifier":
        """Build a DeIdentifier from the person-centric config schema.

        The config groups every fact about an entity together, so we flatten
        persons[].emails / persons[].phones into the ``{value -> token}``
        maps we use at apply time. Duplicate values across persons raise on
        load so configs can't silently shadow each other.
        """
        emails: dict[str, str] = {}
        phones: dict[str, str] = {}

        for person in data.get("persons") or []:
            person_id = person.get("canonical_id", "<unknown>")
            for entry in person.get("emails") or []:
                _add_pii_entry(
                    emails, entry, _normalize_email, kind="email",
                    owner=person_id,
                )
            for entry in person.get("phones") or []:
                _add_pii_entry(
                    phones, entry, _normalize_phone, kind="phone",
                    owner=person_id,
                )

        person_rules = _build_alias_rules(data.get("persons") or [])
        org_rules = _build_alias_rules(data.get("organizations") or [])
        return cls(
            emails=emails,
            phones=phones,
            person_rules=person_rules,
            org_rules=org_rules,
        )

    # ------------------------------------------------------------------ apply

    def apply(
        self, text: str
    ) -> tuple[str, dict[str, int], list[Finding]]:
        """Sanitize ``text`` and return ``(new_text, counts, findings)``.

        ``counts`` is the canonical 4-key shape with totals for mapped
        replacements only:
        ``{"emails": int, "phones": int, "persons": int, "organizations": int}``.
        Unmapped occurrences are not counted here - they still appear as
        Finding entries with ``status == "unmapped"`` so the pipeline can
        emit them to ``pii_quarantine.csv``.

        ``findings`` contains one entry per regex match (mapped + unmapped),
        in left-to-right order of the original text.
        """
        if not isinstance(text, str) or not text:
            return text, empty_replacement_counts(), []

        # Phase 1: scan original text for every transformation.
        findings = self._scan_all_findings(text)

        # Phase 2: cascading replacements (the same order the original
        # design relied on; we ignore the per-pass counts since findings
        # are the source of truth from here on).
        text, _ = _replace(
            text, EMAIL_RE, self.emails, _normalize_email, UNMAPPED_EMAIL
        )
        text, _ = _replace(
            text, PHONE_RE, self.phones, _normalize_phone, UNMAPPED_PHONE
        )
        text = _apply_alias_rules(text, self.person_rules)
        text = _apply_alias_rules(text, self.org_rules)

        # Phase 3: re-render snippets from the sanitized text so they
        # carry no raw PII.
        findings = _attach_sanitized_snippets(findings, sanitized_text=text)

        return text, _counts_from_findings(findings), findings

    def _scan_all_findings(self, text: str) -> list[Finding]:
        """Walk the original text once and produce a Finding per regex
        match, preserving offsets in the original string.

        Spans already covered by a higher-priority match are skipped, which
        gives us:

          - emails win over phones (a phone-shaped substring inside an email
            address isn't double-flagged).
          - persons win over organizations when an alias is shared.
          - longer aliases win over shorter ones inside each kind
            (``Sarah Chen`` is recorded; the ``Sarah`` inside it is not).

        Snippets are intentionally left empty here; they're populated in
        :py:meth:`apply` after all replacement passes have run, so the
        snippet contains no raw PII from neighbouring matches.
        """
        findings: list[Finding] = []
        covered: list[tuple[int, int]] = []

        for m in EMAIL_RE.finditer(text):
            if _within_any_span(m.start(), m.end(), covered):
                continue
            covered.append((m.start(), m.end()))
            normalized = _normalize_email(m.group(0))
            mapped = self.emails.get(normalized)
            findings.append(_finding_for(
                start=m.start(), end=m.end(),
                kind="email",
                value=normalized,
                token=mapped if mapped is not None else UNMAPPED_EMAIL,
                status=STATUS_MAPPED if mapped is not None else STATUS_UNMAPPED,
            ))

        for m in PHONE_RE.finditer(text):
            if _within_any_span(m.start(), m.end(), covered):
                continue
            covered.append((m.start(), m.end()))
            normalized = _normalize_phone(m.group(0))
            mapped = self.phones.get(normalized)
            findings.append(_finding_for(
                start=m.start(), end=m.end(),
                kind="phone",
                value=normalized,
                token=mapped if mapped is not None else UNMAPPED_PHONE,
                status=STATUS_MAPPED if mapped is not None else STATUS_UNMAPPED,
            ))

        for kind, rules in (
            ("person", self.person_rules),
            ("organization", self.org_rules),
        ):
            for rule in rules:
                for m in rule.pattern.finditer(text):
                    if _within_any_span(m.start(), m.end(), covered):
                        continue
                    covered.append((m.start(), m.end()))
                    findings.append(_finding_for(
                        start=m.start(), end=m.end(),
                        kind=kind,
                        value=m.group(0),
                        token=rule.token,
                        status=STATUS_MAPPED,
                    ))

        findings.sort(key=lambda f: f.start_offset)
        return findings


# ---------------------------------------------------------------- internals


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _normalize_phone(value: str) -> str:
    """Reduce a phone to digits (and an optional leading '+').

    This collapses every supported separator format to a single canonical
    representation so the same number written three different ways still
    maps to the same configured token.
    """
    value = value.strip()
    plus = "+" if value.startswith("+") else ""
    digits = re.sub(r"\D", "", value)
    return plus + digits


def _snippet_around(
    text: str, start: int, end: int, window: int = SNIPPET_WINDOW
) -> str:
    """Build a context window around ``[start, end)`` in ``text``, with the
    matched span left as-is (the caller arranges for the matched span in
    ``text`` to already be a token / placeholder so the snippet contains no
    raw PII).

    Whitespace (newlines, tabs, runs of spaces) is collapsed to a single
    space so the snippet stays on one line - keeps CSV rows readable and
    avoids embedding raw newlines in any downstream log format. Leading /
    trailing ellipses signal that the snippet was truncated.
    """
    pre_start = max(0, start - window)
    post_end = min(len(text), end + window)
    leading_ellipsis = "..." if pre_start > 0 else ""
    trailing_ellipsis = "..." if post_end < len(text) else ""
    snippet = f"{leading_ellipsis}{text[pre_start:post_end]}{trailing_ellipsis}"
    return " ".join(snippet.split())


def _replace(
    text: str,
    pattern: re.Pattern[str],
    mapping: dict[str, str],
    normalize,
    placeholder: str,
) -> tuple[str, int]:
    """Apply one replacement pass and return ``(new_text, mapped_count)``.

    Mapped values are replaced with their configured token; unmapped values
    are replaced with the placeholder. Counts here are advisory; the
    pipeline derives counts from findings instead.
    """
    mapped_count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal mapped_count
        normalized = normalize(m.group(0))
        mapped = mapping.get(normalized)
        if mapped is not None:
            mapped_count += 1
            return mapped
        return placeholder

    return pattern.sub(repl, text), mapped_count


def _finding_for(
    *,
    start: int,
    end: int,
    kind: str,
    value: str,
    token: str,
    status: str,
) -> Finding:
    """Build a Finding with empty snippet; snippet is filled in later from
    the fully sanitized text."""
    return Finding(
        kind=kind,
        value=value,
        value_hash=hashlib.sha256(value.encode("utf-8")).hexdigest()[:8],
        token=token,
        status=status,
        snippet="",
        start_offset=start,
        end_offset=end,
    )


def _within_any_span(
    start: int, end: int, spans: list[tuple[int, int]]
) -> bool:
    """Return True if ``[start, end)`` is contained in any span in ``spans``.

    Used so we don't double-flag overlapping matches (e.g. a phone-shaped
    substring living inside an email match, or ``Sarah`` inside an already-
    matched ``Sarah Chen``). The earlier match wins; the later one is
    skipped.
    """
    return any(s <= start and end <= e for s, e in spans)


def _attach_sanitized_snippets(
    findings: list[Finding], *, sanitized_text: str
) -> list[Finding]:
    """Populate each finding's ``snippet`` from the fully sanitized text.

    For each finding (in document order), find the next un-used occurrence
    of its ``token`` in the sanitized text and build a context window
    around it. Because both the regex passes and the alias passes process
    matches in left-to-right order, the Nth finding for a given token in
    the original text corresponds to the Nth occurrence of that token in
    the sanitized text - so a per-token cursor over the sanitized text is
    sufficient.

    This relies on tokens not appearing literally in the input. If a user
    wrote ``PERSON_001`` in their source document, we'd attach a wrong
    snippet, but ``value`` / ``location`` are still correct.
    """
    cursors: dict[str, _Cursor] = {}

    new_findings: list[Finding] = []
    for f in sorted(findings, key=lambda x: x.start_offset):
        cursor = cursors.setdefault(
            f.token, _Cursor(_find_all(f.token, sanitized_text))
        )
        pos = cursor.next()
        snippet = (
            _snippet_around(sanitized_text, pos, pos + len(f.token))
            if pos is not None
            else ""
        )
        new_findings.append(
            Finding(
                kind=f.kind,
                value=f.value,
                value_hash=f.value_hash,
                token=f.token,
                status=f.status,
                snippet=snippet,
                start_offset=f.start_offset,
                end_offset=f.end_offset,
            )
        )
    return new_findings


class _Cursor:
    """Tiny helper: yields positions one at a time, ``None`` when exhausted."""

    __slots__ = ("_positions", "_idx")

    def __init__(self, positions: list[int]) -> None:
        self._positions = positions
        self._idx = 0

    def next(self) -> int | None:
        if self._idx >= len(self._positions):
            return None
        pos = self._positions[self._idx]
        self._idx += 1
        return pos


def _find_all(needle: str, haystack: str) -> list[int]:
    """Return all start positions of ``needle`` in ``haystack``,
    left-to-right, non-overlapping."""
    positions: list[int] = []
    start = 0
    while True:
        pos = haystack.find(needle, start)
        if pos < 0:
            break
        positions.append(pos)
        start = pos + len(needle)
    return positions


def _counts_from_findings(findings: list[Finding]) -> dict[str, int]:
    """Aggregate mapped findings into the canonical 4-key counts dict."""
    counts = empty_replacement_counts()
    kind_to_key = {
        "email": "emails",
        "phone": "phones",
        "person": "persons",
        "organization": "organizations",
    }
    for f in findings:
        if f.status != STATUS_MAPPED:
            continue
        key = kind_to_key.get(f.kind)
        if key is not None:
            counts[key] += 1
    return counts


def _add_pii_entry(
    target: dict[str, str],
    entry: dict,
    normalize,
    *,
    kind: str,
    owner: str,
) -> None:
    """Insert one ``{value, token}`` config entry into a flat lookup.

    Raises ``ValueError`` on missing fields or duplicate values so a
    misconfigured entities.json fails loudly at startup, rather than silently
    redacting things to the wrong token.
    """
    value = entry.get("value")
    token = entry.get("token")
    if not value or not token:
        raise ValueError(
            f"Invalid {kind} entry under {owner!r}: expected "
            f"{{'value': ..., 'token': ...}}, got {entry!r}"
        )
    normalized = normalize(value)
    existing = target.get(normalized)
    if existing is not None and existing != token:
        raise ValueError(
            f"Conflicting {kind} mapping for {value!r} (owner {owner!r}): "
            f"already mapped to {existing}, new token {token}"
        )
    target[normalized] = token


def _build_alias_rules(entities: Iterable[dict]) -> list[_AliasRule]:
    """Compile alias regexes for an entity group, ordered longest-first.

    Sorting by ``-len(alias)`` ensures multi-word aliases (e.g. ``Sarah Chen``)
    are matched before the corresponding shorter alias (``Sarah``). Each alias
    becomes its own compiled regex with non-word lookarounds rather than
    ``\\b``, because some aliases legitimately end in a non-word character
    (e.g. ``Acme Inc.``) and ``\\b`` is undefined at non-word boundaries.
    """
    rules: list[_AliasRule] = []
    for entity in entities:
        token = entity["canonical_id"]
        for alias in entity.get("aliases", []):
            rules.append(
                _AliasRule(
                    alias=alias,
                    token=token,
                    pattern=re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)"),
                )
            )
    rules.sort(key=lambda r: (-len(r.alias), r.alias))
    return rules


def _apply_alias_rules(text: str, rules: list[_AliasRule]) -> str:
    """Apply alias rules in (longest-first) order, returning the new text.

    Per-occurrence accounting is handled by :py:meth:`DeIdentifier._scan_all_findings`
    against the original text; this function just produces the sanitized
    output.
    """
    for rule in rules:
        text = rule.pattern.sub(rule.token, text)
    return text
