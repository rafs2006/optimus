"""Server-side half of the ``OPTIMUS_MEMBER_COMMANDS`` surface setting.

Leaving a command out of ``set_application_commands`` removes it from the
picker, which is cosmetic: a client holding a cached command list can still
send the interaction, and nothing stops a hand-rolled HTTP call. So the setting
is re-checked in ``dispatch_command``, and that refusal is what these tests
pin. The scope factory is never entered on the refusal path, which is the point
-- a disabled command must not open a database transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from optimus.core.config import Settings
from optimus.services.interactions.handlers import InteractionContext
from optimus.services.interactions.logic import CommandError, InteractionRejected
from optimus.services.interactions.service import InteractionService, error_message


def _service(member_commands: tuple[str, ...] | None) -> tuple[InteractionService, list[int]]:
    entered: list[int] = []

    @asynccontextmanager
    async def _scope() -> AsyncIterator[object]:
        entered.append(1)
        yield object()

    service = InteractionService(
        scope=_scope,
        rate_limiter=None,  # type: ignore[arg-type]  # never reached in these tests
        settings=Settings(discord_token="t", member_commands=member_commands),
    )
    return service, entered


def _ctx(command: str) -> InteractionContext:
    return InteractionContext(guild_id=1, user_id=2, member_permissions=0, command=command)


async def test_disabled_member_command_is_refused_without_touching_the_db() -> None:
    service, entered = _service(("report",))

    with pytest.raises(InteractionRejected) as excinfo:
        await service.dispatch_command(_ctx("help"))
    assert excinfo.value.reason is CommandError.COMMAND_DISABLED
    assert entered == []


async def test_enabled_and_moderator_commands_reach_the_handler() -> None:
    service, entered = _service(("report",))

    # Both get past the gate and into _run -- they fail later, inside the real
    # handler against a dummy session, which is beyond what this gate governs.
    for command in ("report", "scamhash"):
        with pytest.raises(Exception) as excinfo:
            await service.dispatch_command(_ctx(command))
        assert not isinstance(excinfo.value, InteractionRejected) or (
            excinfo.value.reason is not CommandError.COMMAND_DISABLED
        )
    assert len(entered) == 2


async def test_default_settings_gate_nothing() -> None:
    service, entered = _service(None)

    # /help is self-contained, so with the gate open it runs to completion --
    # which is the strongest form of "nothing was gated".
    resp = await service.dispatch_command(_ctx("help"))
    assert resp.i18n_key == "command.help"
    assert entered == [1]


def test_refusal_has_a_localized_message() -> None:
    assert error_message(CommandError.COMMAND_DISABLED, "en") == (
        "That command is not available on this server."
    )
    assert error_message(CommandError.COMMAND_DISABLED, "sr") != ""
