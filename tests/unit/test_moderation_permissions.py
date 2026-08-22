"""Local permission maths: the reason enforcement stops calling doomed APIs.

Two properties matter and both are load-bearing. First, the bit constants and
resolution order must match Discord exactly -- a wrong bit would either skip an
action that would have worked or attempt one that cannot. Second, an unknown
answer must fail *open*: a cold cache is never allowed to become the reason a
scam stays up.
"""

from __future__ import annotations

import hikari
import pytest

from optimus.contracts.events import Action
from optimus.services.moderation import permissions as perms
from optimus.services.moderation.failures import FailureKind

# -- constants ---------------------------------------------------------------


def test_local_bit_constants_match_hikari() -> None:
    """Pins every bit against the library's own enum.

    These are hand-written so the module needs no hikari import (keeping it
    unit-testable), which means a typo would be invisible without this test --
    and would silently mis-decide every preflight.
    """
    assert int(hikari.Permissions.ADMINISTRATOR) == perms.ADMINISTRATOR
    assert int(hikari.Permissions.VIEW_CHANNEL) == perms.VIEW_CHANNEL
    assert int(hikari.Permissions.SEND_MESSAGES) == perms.SEND_MESSAGES
    assert int(hikari.Permissions.MANAGE_MESSAGES) == perms.MANAGE_MESSAGES
    assert int(hikari.Permissions.EMBED_LINKS) == perms.EMBED_LINKS
    assert int(hikari.Permissions.READ_MESSAGE_HISTORY) == perms.READ_MESSAGE_HISTORY
    assert int(hikari.Permissions.BAN_MEMBERS) == perms.BAN_MEMBERS
    assert int(hikari.Permissions.KICK_MEMBERS) == perms.KICK_MEMBERS
    assert int(hikari.Permissions.MODERATE_MEMBERS) == perms.MODERATE_MEMBERS


def test_every_named_permission_is_a_real_discord_bit() -> None:
    for bit in perms.PERMISSION_NAMES:
        assert hikari.Permissions(bit)


def test_punitive_requirement_per_action() -> None:
    assert perms.punitive_requirement(Action.DELETE_BAN) == perms.BAN_MEMBERS
    assert perms.punitive_requirement(Action.DELETE_KICK) == perms.KICK_MEMBERS
    assert perms.punitive_requirement(Action.DELETE_TIMEOUT) == perms.MODERATE_MEMBERS
    # A plain delete has no punitive step, so it must require nothing.
    assert perms.punitive_requirement(Action.DELETE) == 0
    assert perms.punitive_requirement(Action.REPORT_ONLY) == 0


# -- check() -----------------------------------------------------------------


def test_unknown_permissions_fail_open() -> None:
    """``None`` means "the cache could not tell us", never "not allowed"."""
    assert perms.check(perms.DELETE_REQUIRES, None).ok is True


def test_administrator_short_circuits_like_discord() -> None:
    result = perms.check(perms.DELETE_REQUIRES, perms.ADMINISTRATOR)
    assert result.ok is True
    assert result.missing == ()


def test_all_required_bits_present_passes() -> None:
    assert perms.check(perms.DELETE_REQUIRES, perms.DELETE_REQUIRES).ok is True


def test_denied_view_channel_is_missing_access_not_missing_permission() -> None:
    """The distinction routes an admin to the right Discord settings page.

    Missing Access is fixed on the channel/category overwrite; a specific
    permission gap is usually fixed on the bot's role. Conflating them is what
    turned a five-second fix into several rounds of debugging.
    """
    result = perms.check(perms.DELETE_REQUIRES, 0)
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind is FailureKind.MISSING_ACCESS
    assert "View Channel" in result.missing


def test_visible_channel_missing_only_manage_messages() -> None:
    result = perms.check(perms.DELETE_REQUIRES, perms.VIEW_CHANNEL)
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind is FailureKind.MISSING_PERMISSION
    assert result.missing == ("Manage Messages",)
    assert result.missing_text == "Manage Messages"


def test_missing_text_lists_every_gap() -> None:
    result = perms.check(perms.REPORT_REQUIRES, perms.VIEW_CHANNEL)
    assert "Send Messages" in result.missing_text
    assert "Embed Links" in result.missing_text


# -- effective_permissions() -------------------------------------------------

_GUILD = 100
_BOT = 7
_MOD_ROLE = 200


def _everyone(*, allow: int = 0, deny: int = 0) -> perms.Overwrite:
    return perms.Overwrite(target_id=_GUILD, is_role=True, allow=allow, deny=deny)


def test_owner_holds_everything_regardless_of_overwrites() -> None:
    granted = perms.effective_permissions(
        role_permissions=[0],
        role_ids=frozenset({_GUILD}),
        member_id=_BOT,
        everyone_id=_GUILD,
        overwrites=[_everyone(deny=perms.VIEW_CHANNEL)],
        is_owner=True,
    )
    assert granted & perms.VIEW_CHANNEL


def test_administrator_bypasses_channel_overwrites() -> None:
    """Discord ignores overwrites for administrators; so must we.

    Otherwise the bot would refuse to attempt a delete it would in fact be
    allowed to perform.
    """
    granted = perms.effective_permissions(
        role_permissions=[perms.ADMINISTRATOR],
        role_ids=frozenset({_GUILD}),
        member_id=_BOT,
        everyone_id=_GUILD,
        overwrites=[_everyone(deny=perms.VIEW_CHANNEL)],
    )
    assert perms.check(perms.DELETE_REQUIRES, granted).ok is True


def test_roles_are_unioned() -> None:
    granted = perms.effective_permissions(
        role_permissions=[perms.VIEW_CHANNEL, perms.MANAGE_MESSAGES],
        role_ids=frozenset({_GUILD, _MOD_ROLE}),
        member_id=_BOT,
        everyone_id=_GUILD,
    )
    assert perms.check(perms.DELETE_REQUIRES, granted).ok is True


def test_everyone_overwrite_applies_before_role_overwrites() -> None:
    """A role allow must be able to restore what @everyone denies.

    This is the exact shape of the reported incident's working channel: the
    category denied the bot, and a role overwrite put it back.
    """
    granted = perms.effective_permissions(
        role_permissions=[perms.VIEW_CHANNEL | perms.MANAGE_MESSAGES],
        role_ids=frozenset({_GUILD, _MOD_ROLE}),
        member_id=_BOT,
        everyone_id=_GUILD,
        overwrites=[
            _everyone(deny=perms.VIEW_CHANNEL),
            perms.Overwrite(target_id=_MOD_ROLE, is_role=True, allow=perms.VIEW_CHANNEL, deny=0),
        ],
    )
    assert granted & perms.VIEW_CHANNEL


def test_role_allow_beats_role_deny() -> None:
    """Discord applies every role deny first, then every role allow."""
    granted = perms.effective_permissions(
        role_permissions=[perms.VIEW_CHANNEL],
        role_ids=frozenset({_GUILD, _MOD_ROLE}),
        member_id=_BOT,
        everyone_id=_GUILD,
        overwrites=[
            perms.Overwrite(target_id=_GUILD, is_role=True, allow=0, deny=perms.VIEW_CHANNEL),
            perms.Overwrite(
                target_id=_MOD_ROLE, is_role=True, allow=perms.VIEW_CHANNEL, deny=perms.VIEW_CHANNEL
            ),
        ],
    )
    assert granted & perms.VIEW_CHANNEL


def test_member_overwrite_wins_over_roles() -> None:
    """The member-specific deny is applied last and is decisive.

    A per-bot deny is precisely how an admin accidentally blinds the bot in one
    channel while its role card still shows every permission granted.
    """
    granted = perms.effective_permissions(
        role_permissions=[perms.VIEW_CHANNEL | perms.MANAGE_MESSAGES],
        role_ids=frozenset({_GUILD, _MOD_ROLE}),
        member_id=_BOT,
        everyone_id=_GUILD,
        overwrites=[
            perms.Overwrite(target_id=_MOD_ROLE, is_role=True, allow=perms.VIEW_CHANNEL, deny=0),
            perms.Overwrite(target_id=_BOT, is_role=False, allow=0, deny=perms.VIEW_CHANNEL),
        ],
    )
    assert not granted & perms.VIEW_CHANNEL
    assert perms.check(perms.DELETE_REQUIRES, granted).failure is not None


def test_overwrites_for_unrelated_targets_are_ignored() -> None:
    granted = perms.effective_permissions(
        role_permissions=[perms.VIEW_CHANNEL | perms.MANAGE_MESSAGES],
        role_ids=frozenset({_GUILD}),
        member_id=_BOT,
        everyone_id=_GUILD,
        overwrites=[
            perms.Overwrite(target_id=999, is_role=True, allow=0, deny=perms.VIEW_CHANNEL),
            perms.Overwrite(target_id=888, is_role=False, allow=0, deny=perms.MANAGE_MESSAGES),
        ],
    )
    assert perms.check(perms.DELETE_REQUIRES, granted).ok is True


def test_matches_hikari_for_a_realistic_overwrite_stack() -> None:
    """Cross-checks the maths against hikari's own permission bit semantics."""
    granted = perms.effective_permissions(
        role_permissions=[
            int(hikari.Permissions.VIEW_CHANNEL | hikari.Permissions.MANAGE_MESSAGES)
        ],
        role_ids=frozenset({_GUILD, _MOD_ROLE}),
        member_id=_BOT,
        everyone_id=_GUILD,
        overwrites=[
            _everyone(deny=int(hikari.Permissions.MANAGE_MESSAGES)),
            perms.Overwrite(
                target_id=_MOD_ROLE,
                is_role=True,
                allow=int(hikari.Permissions.MANAGE_MESSAGES),
                deny=0,
            ),
        ],
    )
    expected = hikari.Permissions.VIEW_CHANNEL | hikari.Permissions.MANAGE_MESSAGES
    assert hikari.Permissions(granted) & expected == expected


# -- preflights --------------------------------------------------------------


class _Probe:
    """Records lookups so tests can assert no needless work was done."""

    def __init__(self, *, channel: int | None = 0, guild: int | None = 0) -> None:
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


@pytest.mark.asyncio
async def test_preflight_delete_reports_missing_access() -> None:
    probe = _Probe(channel=0)
    result = await perms.preflight_delete(probe, _GUILD, 5)
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind is FailureKind.MISSING_ACCESS
    assert probe.channel_calls == 1


@pytest.mark.asyncio
async def test_preflight_delete_passes_when_permitted() -> None:
    probe = _Probe(channel=perms.DELETE_REQUIRES)
    assert (await perms.preflight_delete(probe, _GUILD, 5)).ok is True


@pytest.mark.asyncio
async def test_preflight_delete_fails_open_on_unknown() -> None:
    assert (await perms.preflight_delete(_Probe(channel=None), _GUILD, 5)).ok is True


@pytest.mark.asyncio
async def test_preflight_report_needs_embed_links() -> None:
    probe = _Probe(channel=perms.VIEW_CHANNEL | perms.SEND_MESSAGES)
    result = await perms.preflight_report(probe, _GUILD, 5)
    assert result.ok is False
    assert result.missing == ("Embed Links",)


@pytest.mark.asyncio
async def test_preflight_punitive_checks_guild_scope_only() -> None:
    """Ban/kick/timeout are guild permissions; a channel check would mislead."""
    probe = _Probe(guild=0)
    result = await perms.preflight_punitive(probe, _GUILD, Action.DELETE_BAN)
    assert result.ok is False
    assert result.missing == ("Ban Members",)
    assert probe.channel_calls == 0
    assert probe.guild_calls == 1


@pytest.mark.asyncio
async def test_preflight_punitive_skips_lookup_for_non_punitive_actions() -> None:
    """No requirement means no reason to touch the probe at all."""
    probe = _Probe(guild=0)
    result = await perms.preflight_punitive(probe, _GUILD, Action.DELETE)
    assert result.ok is True
    assert probe.guild_calls == 0
