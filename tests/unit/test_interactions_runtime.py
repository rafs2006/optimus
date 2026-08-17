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
    ) -> tuple[str, str | None]:
        assert events == ["defer"]
        assert received_interaction is interaction
        events.append("dispatch")
        return RESPONSE_MESSAGE, None

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
    ) -> tuple[str, str | None]:
        return RESPONSE_MESSAGE, '{"version": 1, "hashes": []}'

    monkeypatch.setattr(interaction_service, "run_interaction", run_interaction)

    await interaction_service.respond_to_interaction(MagicMock(), interaction)

    interaction.edit_initial_response.assert_awaited_once()
    args, kwargs = interaction.edit_initial_response.await_args
    assert args == (RESPONSE_MESSAGE,)
    attachment = kwargs["attachment"]
    assert attachment.filename == "scamhash-export.json"
    assert attachment.data == b'{"version": 1, "hashes": []}'
