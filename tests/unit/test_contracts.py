"""Tests for event contracts and subjects."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from optimus.contracts import events


def test_subjects_are_versioned() -> None:
    for subject in events.EVENT_SUBJECTS:
        assert subject.startswith("events.")
        assert subject.endswith(".v1")


def test_verdict_event_roundtrip() -> None:
    evt = events.VerdictEvent(
        correlation_id="abc",
        occurred_at=datetime.now(UTC),
        guild_id=1,
        channel_id=2,
        message_id=3,
        attachment_id=4,
        uploader_id=5,
        idempotency_key="3:4",
        verdict=events.Verdict.SCAM,
        confidence=0.9,
        distances={"phash": 0},
    )
    raw = evt.model_dump_json()
    again = events.VerdictEvent.model_validate_json(raw)
    assert again == evt


def test_hashset_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        events.HashSet(phash=1 << 64, dhash=0, whash=0, ahash=0)


def test_event_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        events.MessageImageEvent(
            correlation_id="x",
            occurred_at=datetime.now(UTC),
            guild_id=1,
            channel_id=2,
            message_id=3,
            attachment_id=4,
            uploader_id=5,
            url="https://cdn.example/x.png",
            filename="x.png",
            unexpected="boom",  # type: ignore[call-arg]
        )


def test_confidence_bounds_enforced() -> None:
    with pytest.raises(ValidationError):
        events.VerdictEvent(
            correlation_id="x",
            occurred_at=datetime.now(UTC),
            guild_id=1,
            channel_id=2,
            message_id=3,
            attachment_id=4,
            uploader_id=5,
            idempotency_key="3:4",
            verdict=events.Verdict.CLEAN,
            confidence=1.5,
        )
