"""Tests for the Discord interaction response lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import hikari
import pytest

from optimus.services.interactions import service as interaction_service

RESPONSE_MESSAGE = "Configuration updated."


@pytest.mark.parametrize(
    "interaction_type", [hikari.CommandInteraction, hikari.ComponentInteraction]
)
async def test_interaction_is_deferred_before_dispatch_then_edited(
    monkeypatch: pytest.MonkeyPatch, interaction_type: type[object]
) -> None:
    events: list[str] = []
    interaction = MagicMock(spec=interaction_type)
    interaction.create_initial_response = AsyncMock(
        side_effect=lambda *args, **kwargs: events.append("defer")
    )
    interaction.edit_initial_response = AsyncMock(
        side_effect=lambda *args, **kwargs: events.append("edit")
    )

    async def run_interaction(
        service: object, received_interaction: object
    ) -> tuple[str, str | None, str | None]:
        assert events == ["defer"]
        assert received_interaction is interaction
        events.append("dispatch")
        return RESPONSE_MESSAGE, None, None

    monkeypatch.setattr(interaction_service, "run_interaction", run_interaction)

    await interaction_service.respond_to_interaction(MagicMock(), interaction)

    assert events == ["defer", "dispatch", "edit"]
    interaction.create_initial_response.assert_awaited_once_with(
        hikari.ResponseType.DEFERRED_MESSAGE_CREATE,
        flags=hikari.MessageFlag.EPHEMERAL,
    )
    interaction.edit_initial_response.assert_awaited_once_with(RESPONSE_MESSAGE)


async def test_attachment_body_is_uploaded_as_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``(message, body)`` result must attach the body as a downloadable file.

    Regression: ``/scamhash export`` used to render only the text ("Exported N
    hash(es).") while the JSON body set on ``InteractionResponse.attachment``
    was silently dropped by the glue layer.
    """
    interaction = MagicMock(spec=hikari.CommandInteraction)
    interaction.create_initial_response = AsyncMock()
    interaction.edit_initial_response = AsyncMock()

    async def run_interaction(
        service: object, received_interaction: object
    ) -> tuple[str, str | None, str | None]:
        return RESPONSE_MESSAGE, '{"version": 1, "hashes": []}', None

    monkeypatch.setattr(interaction_service, "run_interaction", run_interaction)

    await interaction_service.respond_to_interaction(MagicMock(), interaction)

    interaction.edit_initial_response.assert_awaited_once()
    args, kwargs = interaction.edit_initial_response.await_args
    assert args == (RESPONSE_MESSAGE,)
    attachment = kwargs["attachment"]
    assert attachment.filename == "scamhash-export.json"
    assert attachment.data == b'{"version": 1, "hashes": []}'


def _review_click(custom_id: str = "om:v1:dismiss:7") -> tuple[MagicMock, MagicMock]:
    interaction = MagicMock(spec=hikari.ComponentInteraction)
    interaction.custom_id = custom_id
    interaction.create_initial_response = AsyncMock()
    interaction.edit_initial_response = AsyncMock()
    interaction.execute = AsyncMock()
    card = MagicMock()
    card.content = None
    card.embeds = [
        hikari.Embed(title="Scam detection #7 — SCAM · 4 images", url="https://discord.com/x")
    ]
    card.edit = AsyncMock()
    interaction.message = card
    return interaction, card


async def test_review_decision_folds_the_card_without_a_private_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decision collapses the card in place; the folded card is the confirmation.

    The press is acknowledged as an update of the card itself, so no private
    "done" message piles up per press, and the buttons are removed so a
    settled card cannot be acted on again.
    """
    interaction, card = _review_click()
    note = "✅ **Dismiss** — handled by <@42>"

    async def run_interaction(
        service: object, received_interaction: object
    ) -> tuple[str, str | None, str | None]:
        return RESPONSE_MESSAGE, None, note

    monkeypatch.setattr(interaction_service, "run_interaction", run_interaction)

    await interaction_service.respond_to_interaction(MagicMock(), interaction)

    interaction.create_initial_response.assert_awaited_once_with(
        hikari.ResponseType.DEFERRED_MESSAGE_UPDATE
    )
    card.edit.assert_awaited_once()
    kwargs = card.edit.await_args.kwargs
    assert kwargs["content"] is None
    assert kwargs["components"] == []
    (folded,) = kwargs["embeds"]
    assert folded.title is None
    assert folded.description == f"**Scam detection #7 — SCAM · 4 images**\n{note}"
    assert folded.url == "https://discord.com/x"
    interaction.edit_initial_response.assert_not_awaited()
    interaction.execute.assert_not_awaited()


async def test_review_refusal_replies_privately_and_leaves_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No decision (permission refused, Discord said no): card untouched, private reply."""
    interaction, card = _review_click("om:v1:unban:7")

    async def run_interaction(
        service: object, received_interaction: object
    ) -> tuple[str, str | None, str | None]:
        return RESPONSE_MESSAGE, None, None

    monkeypatch.setattr(interaction_service, "run_interaction", run_interaction)

    await interaction_service.respond_to_interaction(MagicMock(), interaction)

    card.edit.assert_not_awaited()
    interaction.execute.assert_awaited_once_with(
        RESPONSE_MESSAGE, flags=hikari.MessageFlag.EPHEMERAL
    )
    interaction.edit_initial_response.assert_not_awaited()


def test_folding_an_already_folded_card_keeps_both_decisions() -> None:
    """Two moderators pressing at once: the second line joins the first, once."""
    card = MagicMock()
    first = "**Scam detection #7 — SCAM**\n✅ **Dismiss** — handled by <@1>"
    card.embeds = [hikari.Embed(description=first, url="https://discord.com/x")]
    second = "✅ **Confirm scam** — handled by <@2>"

    folded = interaction_service.folded_card_embed(card, second)
    assert folded.description == f"{first}\n{second}"

    card.embeds = [folded]
    again = interaction_service.folded_card_embed(card, second)
    assert again.description == f"{first}\n{second}"


def test_folding_a_card_without_embeds_still_shows_the_decision() -> None:
    card = MagicMock()
    card.embeds = []
    folded = interaction_service.folded_card_embed(card, "note")
    assert folded.description == "note"


async def test_card_note_edit_failure_does_not_break_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted card (or missing permission) must not fail the interaction."""
    interaction = MagicMock(spec=hikari.ComponentInteraction)
    interaction.create_initial_response = AsyncMock()
    interaction.edit_initial_response = AsyncMock()
    card = MagicMock()
    card.content = ""
    card.edit = AsyncMock(side_effect=RuntimeError("gone"))
    interaction.message = card

    async def run_interaction(
        service: object, received_interaction: object
    ) -> tuple[str, str | None, str | None]:
        return RESPONSE_MESSAGE, None, "note"

    monkeypatch.setattr(interaction_service, "run_interaction", run_interaction)

    await interaction_service.respond_to_interaction(MagicMock(), interaction)

    interaction.edit_initial_response.assert_awaited_once_with(RESPONSE_MESSAGE)
