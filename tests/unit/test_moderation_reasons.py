"""Audit-log reason construction.

The audit log is the only place a moderator can read why optimus removed
someone, so these tests pin the two properties operators actually depend on:
one greppable prefix on every path, and a reason that never exceeds what
Discord will accept.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from optimus.services.moderation.reasons import (
    AUDIT_REASON_LIMIT,
    REASON_PREFIX,
    appeal_approved_reason,
    auto_reason,
    clamp,
    confirmed_reason,
    false_positive_reason,
    manual_unban_reason,
)

ALL_REASONS = (
    auto_reason(confidence=0.97, matched_hash_id="3f9a1c7e", matched_source="guild", message_id=7),
    confirmed_reason(12, matched_hash_id="3f9a1c7e"),
    false_positive_reason(12),
    manual_unban_reason(12),
    appeal_approved_reason(12),
)


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_every_reason_shares_the_greppable_prefix(reason: str) -> None:
    # Operators filter audit-log exports by this prefix; a path that words its
    # own cause differently drops out of that filter and out of the record.
    assert reason.startswith(REASON_PREFIX)


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_every_reason_fits_the_discord_header(reason: str) -> None:
    assert len(quote(reason)) <= AUDIT_REASON_LIMIT


def test_auto_reason_carries_the_match_evidence() -> None:
    reason = auto_reason(
        confidence=0.9712,
        matched_hash_id="3f9a1c7e",
        matched_source="guild",
        message_id=1234567890,
    )
    assert "auto-enforced" in reason
    assert "conf 0.97" in reason
    assert "hash 3f9a1c7e" in reason
    assert "src guild" in reason
    assert "msg 1234567890" in reason


def test_auto_reason_never_claims_a_detection_id() -> None:
    # In the automated path the detection row is written *after* enforcement
    # returns, so no id exists when the ban is issued. Printing one would mean
    # inventing it, and a moderator would look for a row that does not exist.
    assert "det #" not in auto_reason(confidence=0.5)


def test_auto_reason_degrades_when_evidence_is_missing() -> None:
    # A verdict with no hash match (OCR/QR risk scan) still has to produce a
    # usable reason rather than a string of empty separators.
    reason = auto_reason()
    assert reason == f"{REASON_PREFIX} \u2014 optimus auto-enforced"


def test_auto_and_confirmed_are_distinguishable() -> None:
    # Whether a human confirmed the removal is the single most important thing
    # a later reviewer wants to know from the log.
    assert auto_reason(confidence=1.0) != confirmed_reason(1)
    assert "auto-enforced" in auto_reason(confidence=1.0)
    assert "confirmed by moderator" in confirmed_reason(1)


def test_confirmed_reason_records_the_detection_id() -> None:
    assert "det #12" in confirmed_reason(12)


def test_clamp_trims_an_overlong_reason_to_the_encoded_limit() -> None:
    reason = clamp("x" * 900)
    assert len(quote(reason)) <= AUDIT_REASON_LIMIT
    assert reason  # trimmed, not emptied


def test_clamp_measures_encoded_length_not_character_count() -> None:
    # Each of these expands to nine encoded characters, so ~57 of them already
    # exceed the limit despite being far under 512 raw characters.
    reason = clamp("\U0001f600" * 200)
    assert len(reason) < 200
    assert len(quote(reason)) <= AUDIT_REASON_LIMIT


def test_clamp_leaves_a_short_reason_untouched() -> None:
    short = f"{REASON_PREFIX} \u2014 fine"
    assert clamp(short) == short


def test_clamp_keeps_the_prefix_when_a_long_hash_id_overflows() -> None:
    # Truncation eats the tail, so the prefix and the verdict survive even when
    # a pathological fingerprint id blows the budget.
    reason = auto_reason(confidence=0.5, matched_hash_id="f" * 800)
    assert reason.startswith(REASON_PREFIX)
    assert "auto-enforced" in reason
    assert len(quote(reason)) <= AUDIT_REASON_LIMIT
