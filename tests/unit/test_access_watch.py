"""The bot notices when it *regains* access, and rescans only then.

These cover the transition logic, not hikari: the events are reduced to the two
overwrite sets and the two role-permission integers before they reach the
watcher, so every branch is exercised without a gateway.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from optimus.services.gateway.access_watch import AccessWatcher, gained_access
from optimus.services.moderation.permissions import (
    MANAGE_MESSAGES,
    READ_MESSAGE_HISTORY,
    VIEW_CHANNEL,
    Overwrite,
)

_GUILD = 1402357722430570498
_CHANNEL = 1402887429324673035
_BOT = 1409938900482396400
_ROLE = 555

#: What the bot needs to read a channel's history at all.
_RESCAN = VIEW_CHANNEL | READ_MESSAGE_HISTORY


class _State:
    """Stands in for the cache-backed probe's view of the bot's own roles."""

    def __init__(
        self,
        *,
        role_permissions: dict[int, int] | None = None,
        role_ids: frozenset[int] = frozenset({_ROLE}),
        is_owner: bool = False,
        missing: bool = False,
    ) -> None:
        self._role_permissions = role_permissions or {_ROLE: _RESCAN | MANAGE_MESSAGES}
        self._role_ids = role_ids
        self._is_owner = is_owner
        self._missing = missing
        self.calls = 0

    async def role_state(self, guild_id: int) -> tuple[frozenset[int], dict[int, int], bool] | None:
        self.calls += 1
        if self._missing:
            return None
        return self._role_ids, self._role_permissions, self._is_owner


class _Rescan:
    """Records rescan requests; optionally blows up to prove failures are contained."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[int, list[int]]] = []
        self._fail = fail

    async def __call__(self, guild_id: int, channel_ids: Any) -> None:
        self.calls.append((guild_id, list(channel_ids)))
        if self._fail:
            raise RuntimeError("rescan exploded")


def _deny(bits: int) -> list[Overwrite]:
    """An @everyone overwrite denying ``bits`` (how the incident channel looked)."""
    return [Overwrite(target_id=_GUILD, is_role=True, allow=0, deny=bits)]


def _watcher(state: _State, rescan: _Rescan) -> AccessWatcher:
    # debounce 0 keeps the timer path real while making the wait negligible.
    return AccessWatcher(state, rescan, bot_user_id=_BOT, debounce_seconds=0)


# -- the pure transition ------------------------------------------------------


def test_gained_access_true_when_deny_is_lifted() -> None:
    assert gained_access(
        role_ids=frozenset({_ROLE}),
        role_permissions={_ROLE: _RESCAN},
        is_owner=False,
        member_id=_BOT,
        guild_id=_GUILD,
        before=_deny(VIEW_CHANNEL),
        after=[],
    )


def test_gained_access_false_when_already_allowed() -> None:
    """A rename or an unrelated overwrite edit must not trigger a rescan."""
    assert not gained_access(
        role_ids=frozenset({_ROLE}),
        role_permissions={_ROLE: _RESCAN},
        is_owner=False,
        member_id=_BOT,
        guild_id=_GUILD,
        before=[],
        after=[],
    )


def test_gained_access_false_when_still_blocked() -> None:
    """Granting View Channel but not Read Message History is not enough."""
    assert not gained_access(
        role_ids=frozenset({_ROLE}),
        role_permissions={_ROLE: VIEW_CHANNEL},
        is_owner=False,
        member_id=_BOT,
        guild_id=_GUILD,
        before=_deny(VIEW_CHANNEL),
        after=[],
    )


# -- channel overwrite edits --------------------------------------------------


@pytest.mark.asyncio
async def test_channel_update_queues_and_rescans() -> None:
    state, rescan = _State(), _Rescan()
    watcher = _watcher(state, rescan)

    assert await watcher.channel_updated(_GUILD, _CHANNEL, before=_deny(VIEW_CHANNEL), after=[])
    await watcher.flush()

    assert rescan.calls == [(_GUILD, [_CHANNEL])]


@pytest.mark.asyncio
async def test_channel_update_without_prior_state_does_nothing() -> None:
    """No ``old_channel`` means the transition is unknowable -- stay quiet."""
    state, rescan = _State(), _Rescan()
    watcher = _watcher(state, rescan)

    assert not await watcher.channel_updated(_GUILD, _CHANNEL, before=None, after=[])
    await watcher.flush()

    assert rescan.calls == []
    assert state.calls == 0


@pytest.mark.asyncio
async def test_channel_update_without_cached_roles_does_nothing() -> None:
    rescan = _Rescan()
    watcher = _watcher(_State(missing=True), rescan)

    assert not await watcher.channel_updated(_GUILD, _CHANNEL, before=_deny(VIEW_CHANNEL), after=[])
    await watcher.flush()

    assert rescan.calls == []


# -- role permission edits ----------------------------------------------------


@pytest.mark.asyncio
async def test_role_update_queues_only_the_channels_that_flipped() -> None:
    """One role edit, three channels: only the ones that actually unblock."""
    state = _State(role_permissions={_ROLE: 0})
    rescan = _Rescan()
    watcher = _watcher(state, rescan)

    queued = await watcher.role_updated(
        _GUILD,
        _ROLE,
        before_permissions=0,
        after_permissions=_RESCAN,
        channels=[
            (10, []),  # flips: nothing denies it
            (11, _deny(VIEW_CHANNEL)),  # stays blocked: channel-level deny wins
            (12, []),  # flips
        ],
    )
    await watcher.flush()

    assert queued == [10, 12]
    assert rescan.calls == [(_GUILD, [10, 12])]


@pytest.mark.asyncio
async def test_role_update_ignores_a_role_the_bot_does_not_hold() -> None:
    """Editing some other role cannot change the bot's own access."""
    state = _State(role_ids=frozenset({_ROLE}))
    rescan = _Rescan()
    watcher = _watcher(state, rescan)

    assert (
        await watcher.role_updated(
            _GUILD,
            999,
            before_permissions=0,
            after_permissions=_RESCAN,
            channels=[(10, [])],
        )
        == []
    )
    assert rescan.calls == []


@pytest.mark.asyncio
async def test_role_update_without_prior_permissions_does_nothing() -> None:
    rescan = _Rescan()
    watcher = _watcher(_State(), rescan)

    assert (
        await watcher.role_updated(
            _GUILD,
            _ROLE,
            before_permissions=None,
            after_permissions=_RESCAN,
            channels=[(10, [])],
        )
        == []
    )
    assert rescan.calls == []


# -- debounce and shutdown ----------------------------------------------------


@pytest.mark.asyncio
async def test_burst_of_fixes_collapses_into_one_rescan() -> None:
    """A moderator fixing three channels in a row costs one rescan, not three."""
    state, rescan = _State(), _Rescan()
    watcher = AccessWatcher(state, rescan, bot_user_id=_BOT, debounce_seconds=0.05)

    for channel_id in (10, 11, 12):
        await watcher.channel_updated(_GUILD, channel_id, before=_deny(VIEW_CHANNEL), after=[])
    await asyncio.sleep(0.15)

    assert rescan.calls == [(_GUILD, [10, 11, 12])]


@pytest.mark.asyncio
async def test_a_failing_rescan_does_not_escape_the_listener() -> None:
    watcher = _watcher(_State(), _Rescan(fail=True))

    await watcher.channel_updated(_GUILD, _CHANNEL, before=_deny(VIEW_CHANNEL), after=[])
    await watcher.flush()  # must not raise


@pytest.mark.asyncio
async def test_aclose_cancels_a_pending_rescan() -> None:
    state, rescan = _State(), _Rescan()
    watcher = AccessWatcher(state, rescan, bot_user_id=_BOT, debounce_seconds=10)

    await watcher.channel_updated(_GUILD, _CHANNEL, before=_deny(VIEW_CHANNEL), after=[])
    await watcher.aclose()

    assert rescan.calls == []
