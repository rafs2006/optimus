"""The cache-backed probe: right answers, and as close to zero requests as possible.

Two things are being defended. The probe must reproduce Discord's own verdict
from cached state, and it must not turn a permission question into network
traffic -- the failure it replaces produced one rejected request per scam image
in a channel the bot could not see.
"""

from __future__ import annotations

from typing import Any

import hikari
import pytest

from optimus.services.gateway.permission_probe import CachePermissionProbe
from optimus.services.moderation import permissions as perms

_GUILD = 100
_BOT = 7
_MOD_ROLE = 200
_CHANNEL = 300


class _Role:
    def __init__(self, permissions: int) -> None:
        self.permissions = permissions


class _Guild:
    def __init__(self, owner_id: int = 999) -> None:
        self.owner_id = owner_id


class _Member:
    def __init__(self, role_ids: list[int]) -> None:
        self.role_ids = role_ids


class _Channel:
    def __init__(self, overwrites: list[Any]) -> None:
        self.permission_overwrites = {int(o.id): o for o in overwrites}


def _overwrite(target: int, *, role: bool = True, allow: int = 0, deny: int = 0) -> Any:
    return hikari.PermissionOverwrite(
        id=hikari.Snowflake(target),
        type=(
            hikari.PermissionOverwriteType.ROLE if role else hikari.PermissionOverwriteType.MEMBER
        ),
        allow=hikari.Permissions(allow),
        deny=hikari.Permissions(deny),
    )


class _Cache:
    """A minimal stand-in for hikari's gateway cache."""

    def __init__(
        self,
        *,
        guild: _Guild | None = None,
        member: _Member | None = None,
        roles: dict[int, _Role] | None = None,
        channels: dict[int, _Channel] | None = None,
    ) -> None:
        self._guild = guild
        self._member = member
        self._roles = roles or {}
        self._channels = channels or {}

    def get_guild(self, guild_id: int) -> _Guild | None:
        return self._guild

    def get_member(self, guild_id: int, user_id: int) -> _Member | None:
        return self._member

    def get_roles_view_for_guild(self, guild_id: int) -> dict[int, _Role]:
        return self._roles

    def get_guild_channel(self, channel_id: int) -> _Channel | None:
        return self._channels.get(channel_id)

    def get_guild_channels_view_for_guild(self, guild_id: int) -> dict[int, _Channel]:
        return self._channels


class _Rest:
    """Records ``fetch_my_member`` calls; raises when the bot is not a member."""

    def __init__(self, member: _Member | None = None) -> None:
        self._member = member
        self.calls = 0

    async def fetch_my_member(self, guild_id: int) -> _Member:
        self.calls += 1
        if self._member is None:
            raise hikari.ForbiddenError(url="u", headers={}, raw_body=b"")
        return self._member


def _probe(cache: _Cache, rest: _Rest | None = None) -> CachePermissionProbe:
    return CachePermissionProbe(cache, rest or _Rest(_Member([_MOD_ROLE])), bot_user_id=_BOT)


def _full_cache(
    *, overwrites: list[Any] | None = None, role_permissions: int | None = None
) -> _Cache:
    granted = (
        role_permissions
        if role_permissions is not None
        else perms.VIEW_CHANNEL | perms.MANAGE_MESSAGES | perms.BAN_MEMBERS
    )
    return _Cache(
        guild=_Guild(),
        member=_Member([_MOD_ROLE]),
        roles={_GUILD: _Role(0), _MOD_ROLE: _Role(granted)},
        channels={_CHANNEL: _Channel(overwrites or [])},
    )


@pytest.mark.asyncio
async def test_channel_permissions_come_from_cache_without_any_request() -> None:
    rest = _Rest(_Member([_MOD_ROLE]))
    probe = _probe(_full_cache(), rest)
    granted = await probe.channel_permissions(_GUILD, _CHANNEL)
    assert granted is not None
    assert perms.check(perms.DELETE_REQUIRES, granted).ok is True
    assert rest.calls == 0


@pytest.mark.asyncio
async def test_denied_view_channel_is_detected_locally() -> None:
    """The reported incident's failing channel, resolved with zero API calls."""
    rest = _Rest(_Member([_MOD_ROLE]))
    probe = _probe(_full_cache(overwrites=[_overwrite(_GUILD, deny=perms.VIEW_CHANNEL)]), rest)
    granted = await probe.channel_permissions(_GUILD, _CHANNEL)
    result = perms.check(perms.DELETE_REQUIRES, granted)
    assert result.ok is False
    assert "View Channel" in result.missing
    assert rest.calls == 0


@pytest.mark.asyncio
async def test_member_specific_deny_is_honoured() -> None:
    probe = _probe(_full_cache(overwrites=[_overwrite(_BOT, role=False, deny=perms.VIEW_CHANNEL)]))
    granted = await probe.channel_permissions(_GUILD, _CHANNEL)
    assert granted is not None
    assert not granted & perms.VIEW_CHANNEL


@pytest.mark.asyncio
async def test_everyone_role_is_always_included_in_the_base_set() -> None:
    """A bot with no extra roles still inherits @everyone's permissions.

    Without this the base set would be empty and every action would look
    impossible.
    """
    cache = _Cache(
        guild=_Guild(),
        member=_Member([]),
        roles={_GUILD: _Role(perms.VIEW_CHANNEL | perms.MANAGE_MESSAGES)},
        channels={_CHANNEL: _Channel([])},
    )
    granted = await _probe(cache).channel_permissions(_GUILD, _CHANNEL)
    assert perms.check(perms.DELETE_REQUIRES, granted).ok is True


@pytest.mark.asyncio
async def test_guild_permissions_ignore_channel_overwrites() -> None:
    """Ban is a guild permission; a channel deny must not mask it."""
    probe = _probe(_full_cache(overwrites=[_overwrite(_GUILD, deny=perms.BAN_MEMBERS)]))
    granted = await probe.guild_permissions(_GUILD)
    assert granted is not None
    assert granted & perms.BAN_MEMBERS


@pytest.mark.asyncio
async def test_ownership_grants_everything() -> None:
    cache = _full_cache(role_permissions=0)
    cache._guild = _Guild(owner_id=_BOT)
    granted = await _probe(cache).guild_permissions(_GUILD)
    assert granted == perms.ALL_PERMISSIONS


@pytest.mark.asyncio
async def test_uncached_channel_returns_unknown() -> None:
    """Unknown must stay unknown so the preflight fails open and still tries."""
    probe = _probe(_full_cache())
    assert await probe.channel_permissions(_GUILD, 99999) is None


@pytest.mark.asyncio
async def test_uncached_guild_returns_unknown() -> None:
    cache = _full_cache()
    cache._guild = None
    assert await _probe(cache).channel_permissions(_GUILD, _CHANNEL) is None


@pytest.mark.asyncio
async def test_raising_cache_returns_unknown_rather_than_propagating() -> None:
    """A cache error must degrade to "attempt anyway", never break enforcement."""

    class _Broken(_Cache):
        def get_guild(self, guild_id: int) -> _Guild | None:
            raise RuntimeError("cache exploded")

    cache = _Broken(
        guild=_Guild(),
        member=_Member([_MOD_ROLE]),
        roles={_MOD_ROLE: _Role(perms.VIEW_CHANNEL)},
        channels={_CHANNEL: _Channel([])},
    )
    assert await _probe(cache).channel_permissions(_GUILD, _CHANNEL) is None


@pytest.mark.asyncio
async def test_member_is_fetched_once_when_absent_from_cache() -> None:
    """Bots run without the privileged members intent, so this path is the norm.

    One request per guild, memoized -- not one per action.
    """
    cache = _full_cache()
    cache._member = None
    rest = _Rest(_Member([_MOD_ROLE]))
    probe = _probe(cache, rest)
    first = await probe.channel_permissions(_GUILD, _CHANNEL)
    second = await probe.channel_permissions(_GUILD, _CHANNEL)
    third = await probe.guild_permissions(_GUILD)
    assert first == second
    assert third is not None
    assert rest.calls == 1


@pytest.mark.asyncio
async def test_forget_refreshes_roles_after_a_grant() -> None:
    """A newly granted moderator role must take effect without a restart."""
    cache = _full_cache()
    cache._member = None
    rest = _Rest(_Member([_MOD_ROLE]))
    probe = _probe(cache, rest)
    await probe.guild_permissions(_GUILD)
    probe.forget(_GUILD)
    await probe.guild_permissions(_GUILD)
    assert rest.calls == 2


@pytest.mark.asyncio
async def test_failed_member_lookup_returns_unknown() -> None:
    cache = _full_cache()
    cache._member = None
    probe = _probe(cache, _Rest(None))
    assert await probe.channel_permissions(_GUILD, _CHANNEL) is None


@pytest.mark.asyncio
async def test_probe_satisfies_the_protocol_the_preflights_expect() -> None:
    probe: perms.PermissionProbe = _probe(_full_cache())
    assert (await perms.preflight_delete(probe, _GUILD, _CHANNEL)).ok is True


# --- whole-guild audit (/config permissions) ----------------------------------


class _TypedChannel(_Channel):
    """A cached channel that also reports its id and Discord channel type."""

    def __init__(self, channel_id: int, overwrites: list[Any], *, channel_type: int = 0) -> None:
        super().__init__(overwrites)
        self.id = channel_id
        self.type = channel_type


def _guild_cache(channels: dict[int, _TypedChannel], *, granted: int | None = None) -> _Cache:
    cache = _full_cache(role_permissions=granted)
    cache._channels = dict(channels)
    return cache


@pytest.mark.asyncio
async def test_channel_access_covers_every_channel_without_a_request() -> None:
    cache = _guild_cache(
        {
            301: _TypedChannel(301, []),
            302: _TypedChannel(302, [_overwrite(_GUILD, deny=perms.VIEW_CHANNEL)]),
        }
    )
    rest = _Rest(_Member([_MOD_ROLE]))
    probe = _probe(cache, rest)

    access = await probe.channel_access(_GUILD)

    assert access is not None
    granted = dict(access)
    assert perms.check(perms.DELETE_REQUIRES, granted[301]).ok is True
    assert perms.check(perms.DELETE_REQUIRES, granted[302]).ok is False
    assert rest.calls == 0


@pytest.mark.asyncio
async def test_guild_channels_skips_categories_and_voice() -> None:
    """Nothing can be uploaded to them, so they are not access problems."""
    cache = _guild_cache(
        {
            301: _TypedChannel(301, [], channel_type=0),  # text
            302: _TypedChannel(302, [], channel_type=4),  # category
            303: _TypedChannel(303, [], channel_type=2),  # voice
            304: _TypedChannel(304, [], channel_type=11),  # public thread
        }
    )

    channels = _probe(cache).guild_channels(_GUILD)

    assert channels is not None
    assert [channel_id for channel_id, _ in channels] == [301, 304]


@pytest.mark.asyncio
async def test_channel_access_is_unknown_when_no_channels_are_cached() -> None:
    """A cold cache must read as \"cannot check\", never as \"nothing is wrong\"."""
    probe = _probe(_guild_cache({}))

    assert probe.guild_channels(_GUILD) is None
    assert await probe.channel_access(_GUILD) is None


@pytest.mark.asyncio
async def test_role_state_exposes_permissions_per_role() -> None:
    """The access watcher needs per-role bits to recompute a transition."""
    state = await _probe(_full_cache()).role_state(_GUILD)

    assert state is not None
    role_ids, role_permissions, is_owner = state
    assert _MOD_ROLE in role_ids
    assert role_permissions[_MOD_ROLE] & perms.MANAGE_MESSAGES
    assert is_owner is False


@pytest.mark.asyncio
async def test_probe_satisfies_the_channel_inventory_protocol() -> None:
    inventory: perms.ChannelInventory = _probe(_guild_cache({301: _TypedChannel(301, [])}))
    assert await inventory.channel_access(_GUILD) is not None
