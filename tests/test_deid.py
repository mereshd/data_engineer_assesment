"""Unit tests for the de-identifier.

Covers:
  - configured email / phone replacement
  - unmapped email / phone replacement uses placeholder + emits a Finding
  - person and organization alias replacement (incl. trailing period)
  - longer aliases beat shorter ones
  - email pass runs before org pass (domains preserved)
  - recursive walk of nested JSON-like structures (with JSON-path findings)
  - person-centric config schema invariants (validation + duplicates)
  - snippet construction (window, placeholder substitution, whitespace)

Tests are parameterized where the same logic applies to both PII kinds
(email/phone) or both alias kinds (person/organization), so a single
test function locks in the behaviour for every variant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sanitizer.deid import (
    UNMAPPED_EMAIL,
    UNMAPPED_PHONE,
    DeIdentifier,
    Finding,
    _snippet_around,
)
from sanitizer.processors import sanitize_json_value


@pytest.fixture
def deid(minimal_config_path: Path) -> DeIdentifier:
    return DeIdentifier.from_config_path(minimal_config_path)


# --------------------------------------------------------- email / phone


@pytest.mark.parametrize(
    "kind, raw, token, expected_count_key",
    [
        ("email", "Contact john@acme.com about the doc.", "EMAIL_001", "emails"),
        ("phone", "Call +1-212-555-0199 anytime.",        "PHONE_001", "phones"),
    ],
)
def test_known_pii_is_replaced_with_configured_token(
    deid: DeIdentifier,
    kind: str,
    raw: str,
    token: str,
    expected_count_key: str,
) -> None:
    """A configured email/phone becomes its token, gets counted in the
    matching counter, and emits a Finding with status='mapped' so the
    transformations report sees a row."""
    out, counts, findings = deid.apply(raw)
    assert token in out
    assert counts[expected_count_key] == 1
    assert len(findings) == 1
    assert findings[0].kind == kind
    assert findings[0].status == "mapped"
    assert findings[0].token == token


@pytest.mark.parametrize(
    "kind, raw, raw_value, normalized, placeholder",
    [
        (
            "email",
            "Email someone@elsewhere.com please.",
            "someone@elsewhere.com",
            "someone@elsewhere.com",
            UNMAPPED_EMAIL,
        ),
        (
            "phone",
            "Reach me at (415) 555-1212.",
            "(415) 555-1212",
            "4155551212",
            UNMAPPED_PHONE,
        ),
    ],
)
def test_unmapped_pii_uses_placeholder_and_emits_finding(
    deid: DeIdentifier,
    kind: str,
    raw: str,
    raw_value: str,
    normalized: str,
    placeholder: str,
) -> None:
    """Unmapped values: become a placeholder in the sanitized text, are
    NOT counted in ``counts`` (they're not real replacements), emit a
    Finding with status='unmapped', and the same value seen in two
    different ``apply()`` calls gets the same value_hash (cross-document
    dedup signal)."""
    out, counts, findings = deid.apply(raw)
    assert raw_value not in out
    assert placeholder in out
    assert counts[kind + "s"] == 0
    assert len(findings) == 1

    finding = findings[0]
    assert finding.kind == kind
    assert finding.status == "unmapped"
    assert finding.token == placeholder
    assert finding.value == normalized
    assert len(finding.value_hash) == 8
    assert placeholder in finding.snippet
    assert raw_value not in finding.snippet  # snippet uses placeholder

    # Stable cross-call value_hash for the same input.
    _, _, again = deid.apply(f"Second mention: {raw_value}.")
    assert again[0].value_hash == finding.value_hash


def test_iso_dates_are_not_treated_as_phones(deid: DeIdentifier) -> None:
    """Word-boundary anchors keep the phone regex from chewing into ISO
    timestamps like ``2026-05-01T10:05:00Z``."""
    out, counts, findings = deid.apply("Logged on 2026-05-01T10:05:00Z.")
    assert counts["phones"] == 0
    assert findings == []
    assert "2026-05-01T10:05:00Z" in out


# ------------------------------------------------------ person / org alias


@pytest.mark.parametrize(
    "kind, raw, expected_tokens, expected_count_key, expected_count",
    [
        (
            "person",
            "John Miller and Sarah were on the call.",
            {"PERSON_001", "PERSON_002"},
            "persons",
            2,
        ),
        (
            "organization",
            "BetaHealth uses Acme Inc. as a vendor.",
            {"ORG_001", "ORG_002"},
            "organizations",
            2,
        ),
    ],
)
def test_alias_replacement_for_each_kind(
    deid: DeIdentifier,
    kind: str,
    raw: str,
    expected_tokens: set[str],
    expected_count_key: str,
    expected_count: int,
) -> None:
    """Person and organization aliases both follow the same contract:
    every literal alias maps to its canonical token, counted under the
    corresponding key. The org case also exercises the ``Acme Inc.``
    trailing-period alias - lookarounds (not ``\\b``) are required for
    that one."""
    out, counts, _ = deid.apply(raw)
    for token in expected_tokens:
        assert token in out
    assert counts[expected_count_key] == expected_count


def test_longer_alias_wins_over_shorter(deid: DeIdentifier) -> None:
    """``Sarah Chen`` is consumed before the ``Sarah`` alias gets a
    chance to match the leading word."""
    out, counts, _ = deid.apply("Sarah Chen and Sarah are the same person.")
    assert out.count("PERSON_002") == 2
    assert "Chen" not in out
    assert "Sarah" not in out
    assert counts["persons"] == 2


def test_email_replaced_before_org_so_domain_is_preserved_intact(
    deid: DeIdentifier,
) -> None:
    """If the email pass didn't run first, ``BetaHealth`` would corrupt
    ``sarah@betahealth.io`` mid-replacement. Order of passes matters."""
    out, counts, _ = deid.apply("Mail goes to sarah@betahealth.io next week.")
    assert "sarah@betahealth.io" not in out
    assert "BetaHealth" not in out
    assert "EMAIL_002" in out
    assert counts["emails"] == 1
    assert counts["organizations"] == 0


def test_alias_does_not_match_inside_other_words(deid: DeIdentifier) -> None:
    """``Mark`` doesn't match inside ``Marketing`` (lookaround anchors)."""
    out, counts, _ = deid.apply("Marketing was discussed by Mark.")
    assert "Marketing" in out
    assert counts["persons"] == 0


# ---------------------------------------------------------- json walker


def test_recursive_json_walk_preserves_structure_and_nonstrings(
    deid: DeIdentifier,
) -> None:
    """Only string *values* are sanitized; numbers/bools/null pass
    through. Mapped findings carry status='mapped' and a real token."""
    payload = {
        "id": "ACME-101",
        "issue": {
            "summary": "BetaHealth onboarding import failing",
            "reporter": "Sarah Chen",
            "comments": [
                {"author": "John Miller", "body": "From sarah@betahealth.io."},
                {"author": "Mark", "body": "Plain text without entities."},
            ],
            "priority": 3,
            "resolved": False,
            "due": None,
        },
    }
    out, counts, findings = sanitize_json_value(payload, deid)

    # Non-string values pass through untouched.
    assert out["id"] == "ACME-101"
    assert out["issue"]["priority"] == 3
    assert out["issue"]["resolved"] is False
    assert out["issue"]["due"] is None

    # String values get sanitized at every depth.
    assert out["issue"]["summary"] == "ORG_001 onboarding import failing"
    assert out["issue"]["reporter"] == "PERSON_002"
    assert out["issue"]["comments"][0]["author"] == "PERSON_001"
    assert out["issue"]["comments"][0]["body"] == "From EMAIL_002."

    assert counts["emails"] == 1
    assert counts["persons"] >= 2
    assert counts["organizations"] == 1
    assert findings, "expected mapped findings"
    assert all(f.status == "mapped" for f in findings)


def test_recursive_json_walk_attaches_path_to_unmapped_findings(
    deid: DeIdentifier,
) -> None:
    """JSON-path location strings let a reviewer jump to the exact
    field a finding was discovered in."""
    payload = {
        "issues": [
            {"summary": "ok"},
            {
                "summary": "Contact unknown@vendor.example.",
                "comments": [{"body": "also call (415) 555-2200."}],
            },
        ]
    }
    _, _, findings = sanitize_json_value(payload, deid)
    by_kind = {f.kind: f for f in findings}
    assert by_kind["email"].location_hint == "$.issues[1].summary"
    assert by_kind["phone"].location_hint == "$.issues[1].comments[0].body"


# ---------------------------------------------------------- idempotency


def test_apply_is_idempotent_on_already_sanitized_text(
    deid: DeIdentifier,
) -> None:
    """Feeding the sanitizer's own output back in is a no-op."""
    sanitized, _, _ = deid.apply(
        "PERSON_001 spoke with PERSON_002 from ORG_001."
    )
    again, counts, findings = deid.apply(sanitized)
    assert again == sanitized
    assert counts == {
        "emails": 0,
        "phones": 0,
        "persons": 0,
        "organizations": 0,
    }
    assert findings == []


# ------------------------------------------------------- config schema


def _person_only_config(person: dict) -> dict:
    return {"persons": [person], "organizations": []}


def test_duplicate_email_across_persons_raises() -> None:
    """Same email mapped to two tokens under two different persons must
    fail loudly at load time, not silently last-writer-wins."""
    cfg = {
        "persons": [
            {
                "canonical_id": "PERSON_A",
                "aliases": ["Alice"],
                "emails": [{"value": "shared@example.com", "token": "EMAIL_A"}],
                "phones": [],
            },
            {
                "canonical_id": "PERSON_B",
                "aliases": ["Bob"],
                "emails": [{"value": "shared@example.com", "token": "EMAIL_B"}],
                "phones": [],
            },
        ],
        "organizations": [],
    }
    with pytest.raises(ValueError, match="Conflicting email mapping"):
        DeIdentifier.from_config_dict(cfg)


def test_email_entry_missing_token_raises() -> None:
    """A malformed PII entry (missing ``token``) should refuse to load."""
    cfg = _person_only_config({
        "canonical_id": "PERSON_A",
        "aliases": ["Alice"],
        "emails": [{"value": "alice@example.com"}],
        "phones": [],
    })
    with pytest.raises(ValueError, match="Invalid email entry"):
        DeIdentifier.from_config_dict(cfg)


def test_same_email_listed_twice_with_same_token_is_ok() -> None:
    """Idempotent duplicates (same value, same token) must not raise -
    real configs sometimes restate a mapping for clarity, including
    case-insensitive variants of the same address."""
    cfg = _person_only_config({
        "canonical_id": "PERSON_A",
        "aliases": ["Alice"],
        "emails": [
            {"value": "alice@example.com", "token": "EMAIL_A"},
            {"value": "ALICE@example.com", "token": "EMAIL_A"},
        ],
        "phones": [],
    })
    deid = DeIdentifier.from_config_dict(cfg)
    out, counts, _ = deid.apply("Email Alice at alice@example.com.")
    assert "EMAIL_A" in out
    assert counts["emails"] == 1


def test_repository_config_loads_cleanly(project_root: Path) -> None:
    """Smoke test against the real repo config so a regression in shape
    expectations is caught here, not via a confusing pipeline error."""
    cfg_path = project_root / "config" / "entities.json"
    deid = DeIdentifier.from_config_path(cfg_path)
    out, counts, _ = deid.apply(
        "Sarah Chen at sarah@betahealth.io, +1-212-555-0199."
    )
    assert "EMAIL_002" in out
    assert "PHONE_001" in out
    assert "PERSON_002" in out
    assert counts == {"emails": 1, "phones": 1, "persons": 1, "organizations": 0}


# ---------------------------------------------------------------- snippets


def test_snippet_around_window_truncates_and_collapses_whitespace() -> None:
    """``_snippet_around`` is a pure context-window helper - it does NOT
    do any substitution of its own. It collapses whitespace so the
    snippet stays single-line and adds ellipses when truncating."""
    # Newlines collapsed to single space; full content fits, no ellipsis.
    short = "Line one.\nReach <UNMAPPED_EMAIL> for help.\nLine three."
    start = short.index("<UNMAPPED_EMAIL>")
    snippet = _snippet_around(short, start, start + len("<UNMAPPED_EMAIL>"))
    assert "<UNMAPPED_EMAIL>" in snippet
    assert "\n" not in snippet

    # Long surrounding context gets ellipsis on both sides.
    long = "X" * 100 + "<UNMAPPED_EMAIL>" + "Y" * 100
    truncated = _snippet_around(long, 100, 100 + len("<UNMAPPED_EMAIL>"), window=10)
    assert truncated.startswith("...")
    assert truncated.endswith("...")
    assert "<UNMAPPED_EMAIL>" in truncated


def test_snippet_against_sanitized_text_contains_no_other_raw_pii(
    deid: DeIdentifier,
) -> None:
    """End-to-end: when one document has both an unmapped email and an
    unmapped phone, neither finding's snippet should contain the other's
    raw value (because snippets are taken from the fully sanitized
    text)."""
    raw = (
        "External vendor not yet onboarded: ops@externalvendor.example.\n"
        "Their hotline: (415) 555-1212.\n"
    )
    _, _, findings = deid.apply(raw)
    by_kind = {f.kind: f for f in findings}
    assert "ops@externalvendor.example" not in by_kind["phone"].snippet
    assert "(415) 555-1212" not in by_kind["email"].snippet
    assert UNMAPPED_PHONE in by_kind["email"].snippet
    assert UNMAPPED_EMAIL in by_kind["phone"].snippet


def test_finding_to_dict_round_trip() -> None:
    """``Finding.to_dict`` is the contract the pipeline uses to write
    rows - keep the field set locked in."""
    f = Finding(
        kind="email",
        value="x@y.com",
        value_hash="abcd1234",
        token="EMAIL_007",
        status="mapped",
        snippet="...",
        start_offset=10,
        end_offset=18,
    )
    assert f.to_dict() == {
        "kind": "email",
        "value": "x@y.com",
        "value_hash": "abcd1234",
        "token": "EMAIL_007",
        "status": "mapped",
        "snippet": "...",
        "start_offset": 10,
        "end_offset": 18,
    }
