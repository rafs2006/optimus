"""Wiring check: the command layer's blocker text comes from the real probe.

The handler tests use a fake, so these assertions cover the seam between
``DbDeps`` and the permission maths -- that the right scope is checked for each
policy, and that an absent or unhelpful probe degrades to silence rather than to
a false "blocked" claim.
"""

from __future__ import annotations

import pytest

from optimus.core.config import get_settings
from optimus.core.ratelimit import InMemoryRateLimiter
from optimus.services.interactions.service import DbDeps
from optimus.services.moderation import permissions as perms

_GUILD = 1
_CHANNEL = 1402887429324673035


class _StubProbe:
    def __init__(self, *, channel: int | None, guild: int | None) -> None:
        self._channel = channel
        self._guild = guild
        self.channel_calls = 0
        self.guild_calls = 0

    async def channel_permissions(self, guild_id: int, channel_id: int) -> int | None:
        self.channel_calls += 1
        return self._channel

    async def guild_permissions(self, guild_id: int) -> int | None:
        self.guild_calls += 1
        return self._guild


def _deps(probe: object | None) -> DbDeps:
    # No database work happens on this path, so a session is never touched.
    return DbDeps(None, InMemoryRateLimiter(), get_settings(), probe=probe)  # type: ignore[arg-type]


async def _blocked(probe: object | None, action: str = "delete_ban") -> str | None:
    return await _deps(probe).enforcement_blocked(_GUILD, _CHANNEL, action=action, locale="en")


@pytest.mark.asyncio
async def test_invisible_channel_is_reported_with_the_channel_and_permission() -> None:
    reason = await _blocked(_StubProbe(channel=0, guild=perms.BAN_MEMBERS))
    assert reason is not None
    assert f"<#{_CHANNEL}>" in reason
    assert "View Channel" in reason


@pytest.mark.asyncio
async def test_missing_ban_permission_is_reported_without_a_channel() -> None:
    """Ban is guild-scoped; naming a channel would send an admin to the wrong page."""
    reason = await _blocked(_StubProbe(channel=perms.DELETE_REQUIRES, guild=0))
    assert reason is not None
    assert "Ban Members" in reason
    assert f"<#{_CHANNEL}>" not in reason


@pytest.mark.asyncio
async def test_delete_blocker_is_reported_first() -> None:
    """The visible harm leads: a moderator fixes the channel they are looking at."""
    reason = await _blocked(_StubProbe(channel=0, guild=0))
    assert reason is not None
    assert "View Channel" in reason
    assert "Ban Members" not in reason


@pytest.mark.asyncio
async def test_nothing_is_reported_when_the_bot_can_act() -> None:
    probe = _StubProbe(channel=perms.DELETE_REQUIRES, guild=perms.BAN_MEMBERS)
    assert await _blocked(probe) is None
    assert (probe.channel_calls, probe.guild_calls) == (1, 1)


@pytest.mark.asyncio
async def test_unknown_permissions_report_nothing() -> None:
    """Silence means "unknown", never "blocked" -- the reply stays as it was."""
    assert await _blocked(_StubProbe(channel=None, guild=None)) is None


@pytest.mark.asyncio
async def test_without_a_probe_the_reply_is_unchanged() -> None:
    assert await _blocked(None) is None


@pytest.mark.asyncio
async def test_a_plain_delete_policy_checks_only_the_channel() -> None:
    probe = _StubProbe(channel=perms.DELETE_REQUIRES, guild=0)
    assert await _blocked(probe, action="delete") is None
    assert probe.guild_calls == 0


@pytest.mark.asyncio
async def test_an_unrecognised_policy_string_is_ignored() -> None:
    """A future or malformed config value must not crash a moderator's command."""
    probe = _StubProbe(channel=0, guild=0)
    assert await _blocked(probe, action="not_a_policy") is None
    assert probe.channel_calls == 0
