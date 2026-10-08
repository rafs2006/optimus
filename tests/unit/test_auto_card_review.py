"""The folded card for a match the bot settled by itself.

It has no buttons, so nothing on it can be misclicked. It keeps the removed
post's ID and uploader as evidence, and mentions ``/queue detection:``, which
posts the report again as a full card with Unban and False positive.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from optimus.i18n import translate
from optimus.shared.review import (
    COMMAND_IDS,
    ReportData,
    build_card,
    command_mention,
    decided_note,
    merge_reports,
)


def _data(**kw: Any) -> ReportData:
    base: dict[str, Any] = {
        "detection_id": 9,
        "guild_id": 1,
        "channel_id": 2,
        "message_id": 3,
        "uploader_id": 42,
        "verdict": "scam",
        "confidence": None,
        "action_taken": "delete_ban",
    }
    base.update(kw)
    return ReportData(**base)


@pytest.fixture
def queue_id() -> Iterator[int]:
    COMMAND_IDS["queue"] = 777
    yield 777
    COMMAND_IDS.pop("queue", None)


def test_folded_card_keeps_the_removed_post_as_evidence() -> None:
    note = decided_note(_data(auto_handled=True))
    assert "[Original message](https://discord.com/channels/1/2/3)" in note
    assert "ID `3`" in note
    assert "<@42>" in note


def test_folded_card_has_no_buttons_and_names_the_queue_command(queue_id: int) -> None:
    embeds, rows = build_card([_data(auto_handled=True)])
    assert rows == []
    assert f"</queue:{queue_id}> `detection:9`" in embeds[0].description


def test_queue_mention_falls_back_to_plain_text_before_registration() -> None:
    assert "queue" not in COMMAND_IDS
    assert command_mention("queue") == "/queue"
    assert "/queue `detection:9`" in decided_note(_data(auto_handled=True))


def test_moderator_folded_card_has_neither_line() -> None:
    note = decided_note(_data(decided_by=77))
    assert "Original message" not in note
    assert "detection:" not in note


def test_hints_are_translated() -> None:
    for key in ("card.original_message", "card.review_hint"):
        assert translate(key, "sr") != translate(key, "en")


# --- One action line per card ----------------------------------------------


def test_merged_card_names_a_repeated_action_once() -> None:
    failed = "delete_ban (failed: missing_permission)"
    tail = "purged 2 more in 2 channels — cleared 4 other report(s)"
    merged = merge_reports([_data(action_taken=failed), _data(action_taken=f"{failed} — {tail}")])
    assert merged.action_taken == f"{failed} — {tail}"


def test_merged_card_keeps_distinct_actions() -> None:
    merged = merge_reports(
        [
            _data(action_taken="delete_ban — purged 1 more in 1 channels"),
            _data(action_taken="report_only"),
        ]
    )
    assert merged.action_taken == "delete_ban; report_only — purged 1 more in 1 channels"


def test_merged_card_drops_a_delete_the_ban_already_covers() -> None:
    # A ban is per user: once one image banned, "delete" adds nothing.
    merged = merge_reports(
        [
            _data(action_taken="delete_ban — purged 1 more in 1 channels"),
            _data(action_taken="delete"),
        ]
    )
    assert merged.action_taken == "delete_ban — purged 1 more in 1 channels"
