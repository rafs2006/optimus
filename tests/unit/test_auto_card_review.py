"""Reviewing a card the bot settled by itself.

A fully handled match is posted as a small folded card with one grey Review
button and a link to the removed post. Review opens the card in place with
Unban (when a ban happened), False positive and Dismiss; Dismiss folds it back
unchanged, so an accidental open costs nothing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import hikari
import pytest

from optimus.i18n import translate
from optimus.services.interactions import service as interaction_service
from optimus.services.interactions.handlers import (
    CardEdit,
    CardEditKind,
    DetectionFacts,
    InteractionContext,
    handle_review_button,
)
from optimus.services.moderation.review import (
    ParsedCustomId,
    ReportData,
    ReviewAction,
    build_card,
    build_refolded_card,
    build_reopened_card,
    decided_note,
    reopened_buttons,
    split_folded,
)
from tests.unit.test_interactions_handlers import MANAGE_MSGS, FakeDeps


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


def _ids(rows: list[object]) -> list[str]:
    return [c.custom_id for row in rows for c in row.components]  # type: ignore[attr-defined]


# --- The folded card --------------------------------------------------------


def test_folded_card_keeps_the_removed_post_as_evidence() -> None:
    note = decided_note(_data(auto_handled=True))
    assert "[Original message](https://discord.com/channels/1/2/3)" in note
    assert "ID `3`" in note
    assert "<@42>" in note


def test_moderator_folded_card_has_no_removed_post_hint() -> None:
    note = decided_note(_data(decided_by=77))
    assert "Original message" not in note


def test_folded_card_has_one_grey_review_button() -> None:
    _embeds, rows = build_card([_data(auto_handled=True)])
    assert _ids(rows) == ["om:v1:review:9"]
    (button,) = rows[0].components  # type: ignore[attr-defined]
    assert button.style is hikari.ButtonStyle.SECONDARY
    assert button.label == "Review"


# --- Reopened card ----------------------------------------------------------


def test_reopened_buttons_offer_unban_only_after_a_ban() -> None:
    assert reopened_buttons([_data()]) == (
        ReviewAction.UNBAN,
        ReviewAction.FALSE_POSITIVE,
        ReviewAction.REFOLD,
    )
    assert reopened_buttons([_data(action_taken="delete")]) == (
        ReviewAction.FALSE_POSITIVE,
        ReviewAction.REFOLD,
    )


def test_reopened_card_keeps_the_summary_and_shows_the_undo_buttons() -> None:
    embeds, rows = build_reopened_card([_data()], "summary line")
    assert embeds[0].title is not None
    assert embeds[0].description == "summary line"
    assert _ids(rows) == ["om:v1:unban:9", "om:v1:false_positive:9", "om:v1:refold:9"]
    labels = [c.label for row in rows for c in row.components]  # type: ignore[attr-defined]
    assert labels == ["Unban", "False positive", "Dismiss"]


def test_refolded_card_gets_its_review_button_back() -> None:
    embeds, rows = build_refolded_card("Scam detection #9", "summary", "https://x", 9)
    assert embeds[0].description == "**Scam detection #9**\nsummary"
    assert _ids(rows) == ["om:v1:review:9"]


def test_split_folded() -> None:
    assert split_folded("**T**\nbody\nmore") == ("T", "body\nmore")
    assert split_folded("**T**") == ("T", "")
    assert split_folded("no title") == (None, "no title")


# --- Handlers ---------------------------------------------------------------


def _deps() -> FakeDeps:
    det = DetectionFacts(
        detection_id=9,
        channel_id=2,
        message_id=3,
        attachment_id=4,
        uploader_id=42,
        hashes=None,
        verdict="scam",
        action_taken="delete_ban",
    )
    other = DetectionFacts(
        detection_id=10,
        channel_id=2,
        message_id=3,
        attachment_id=5,
        uploader_id=42,
        hashes=None,
        verdict="scam",
        action_taken="delete_ban",
    )
    return FakeDeps(detections={9: det, 10: other}, cards={500: [9, 10]})


def _click() -> InteractionContext:
    return InteractionContext(
        guild_id=1,
        user_id=99,
        member_permissions=MANAGE_MSGS,
        command="",
        card_message_id=500,
    )


@pytest.mark.asyncio
async def test_review_opens_the_card_with_every_image() -> None:
    deps = _deps()
    response = await handle_review_button(_click(), ParsedCustomId(ReviewAction.REVIEW, 9), deps)
    assert response.card_note_key is None
    edit = response.card_edit
    assert edit is not None and edit.kind is CardEditKind.REOPEN
    assert [i.detection_id for i in edit.items] == [9, 10]
    assert all(i.action_taken == "delete_ban" for i in edit.items)
    assert deps.audits == [(1, 99, "review.reopen", "9")]


@pytest.mark.asyncio
async def test_dismiss_on_a_reopened_card_changes_nothing() -> None:
    deps = _deps()
    response = await handle_review_button(_click(), ParsedCustomId(ReviewAction.REFOLD, 9), deps)
    assert response.card_edit == CardEdit(CardEditKind.REFOLD, 9)
    assert response.card_note_key is None
    assert deps.audits == []


# --- Glue: re-render in place, round trip -----------------------------------


def _card(embed: Any) -> MagicMock:
    card = MagicMock()
    card.embeds = [embed]
    card.edit = AsyncMock()
    return card


def test_open_fold_back_and_decide_round_trip() -> None:
    folded_embeds, _ = build_card([_data(auto_handled=True)])
    folded = folded_embeds[0]
    _title, summary = split_folded(folded.description)

    reopened, rows = interaction_service.card_edit_payload(
        _card(folded), CardEdit(CardEditKind.REOPEN, 9, (_data(),))
    )
    assert reopened[0].description == summary
    assert "om:v1:refold:9" in _ids(rows)

    refolded, rows = interaction_service.card_edit_payload(
        _card(reopened[0]), CardEdit(CardEditKind.REFOLD, 9)
    )
    assert refolded[0].description == folded.description
    assert _ids(rows) == ["om:v1:review:9"]

    # A decision on the reopened card keeps the bot's summary and adds who.
    decided = interaction_service.folded_card_embed(_card(reopened[0]), "✅ **Unban** — <@99>")
    assert decided.description.endswith(f"{summary}\n✅ **Unban** — <@99>")


async def test_card_edit_rerenders_in_place_without_a_private_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = MagicMock(spec=hikari.ComponentInteraction)
    interaction.custom_id = "om:v1:review:9"
    interaction.create_initial_response = AsyncMock()
    interaction.edit_initial_response = AsyncMock()
    interaction.execute = AsyncMock()
    folded_embeds, _ = build_card([_data(auto_handled=True)])
    card = _card(folded_embeds[0])
    interaction.message = card

    async def run_interaction(
        service: object, received: object
    ) -> tuple[str, None, None, CardEdit]:
        return "ok", None, None, CardEdit(CardEditKind.REOPEN, 9, (_data(),))

    monkeypatch.setattr(interaction_service, "run_interaction", run_interaction)
    await interaction_service.respond_to_interaction(MagicMock(), interaction)

    card.edit.assert_awaited_once()
    kwargs = card.edit.await_args.kwargs
    assert "om:v1:false_positive:9" in _ids(kwargs["components"])
    interaction.execute.assert_not_awaited()
    interaction.edit_initial_response.assert_not_awaited()


def test_hints_are_translated() -> None:
    assert translate("card.original_message", "sr") != translate("card.original_message", "en")
