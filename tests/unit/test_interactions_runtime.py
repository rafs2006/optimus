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


async def test_card_note_is_appended_to_the_review_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``card_note`` must be appended to the button's source message once.

    The note is what tells the *other* moderators in the shared review channel
    that the report was handled and by whom; a double click (or interaction
    retry) whose note is already present must not append it twice.
    """
    interaction = MagicMock(spec=hikari.ComponentInteraction)
    interaction.create_initial_response = AsyncMock()
    interaction.edit_initial_response = AsyncMock()
    card = MagicMock()
    card.content = "Scam detected: #7"
    card.edit = AsyncMock()
    interaction.message = card

    note = "Confirmed scam — handled by <@42>"

    async def run_interaction(
        service: object, received_interaction: object
    ) -> tuple[str, str | None, str | None]:
        return RESPONSE_MESSAGE, None, note

    monkeypatch.setattr(interaction_service, "run_interaction", run_interaction)

    await interaction_service.respond_to_interaction(MagicMock(), interaction)
    card.edit.assert_awaited_once_with(f"Scam detected: #7\n\n{note}")

    # Second click: the note already sits in the content -> no second edit.
    card.content = f"Scam detected: #7\n\n{note}"
    card.edit.reset_mock()
    await interaction_service.respond_to_interaction(MagicMock(), interaction)
    card.edit.assert_not_awaited()


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
