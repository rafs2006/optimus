"""A cache-backed :class:`PermissionProbe` -- answers "can I do this?" for free.

Asking Discord whether an action is allowed costs a request and returns a
``403``; the gateway already streams every role and channel overwrite into the
local cache, so the same answer is a bitmask computation with no network call at
all. That distinction is the whole point: during a raid in a channel the bot
cannot see, the old code produced one failed request per scam image.

The one piece the cache cannot supply is the bot's own member row: listing guild
members requires the privileged ``GUILD_MEMBERS`` intent, which this bot
deliberately does not request. So the bot's role ids come from a single
``fetch_my_member`` call per guild, memoized for the process lifetime and
invalidated through :meth:`forget`. Roles are cached, channel overwrites are
cached, and the bot's own roles change rarely -- so the steady state is still
zero requests per action.

Anything the probe cannot resolve returns ``None``, which the preflight treats
as "attempt anyway": a cold cache or a failed lookup must never be the reason
enforcement silently stops.

Only the channel's own overwrites are consulted, which is what Discord actually
does -- a channel synced to its category carries a materialized copy of the
category's overwrites, and an unsynced channel is governed solely by its own.
Falling back to the parent would invent restrictions Discord does not apply and
could skip a delete that would have succeeded.
"""

from __future__ import annotations

from typing import Any

from optimus.core.logging import get_logger
from optimus.services.moderation.permissions import Overwrite, effective_permissions

_log = get_logger(__name__)


class CachePermissionProbe:
    """Resolves the bot's effective permissions from hikari's gateway cache."""

    def __init__(self, cache: Any, rest: Any, *, bot_user_id: int) -> None:
        self._cache = cache
        self._rest = rest
        self._bot_user_id = bot_user_id
        self._roles: dict[int, frozenset[int]] = {}

    def forget(self, guild_id: int) -> None:
        """Drop the memoized role ids for ``guild_id``.

        Called when the bot's own roles change, so a freshly granted moderator
        role takes effect without a restart.
        """
        self._roles.pop(guild_id, None)

    async def guild_permissions(self, guild_id: int) -> int | None:
        """Guild-wide permissions for the bot, ignoring channel overwrites."""
        state = await self._guild_state(guild_id)
        if state is None:
            return None
        role_ids, role_perms, is_owner = state
        return effective_permissions(
            role_permissions=role_perms,
            role_ids=role_ids,
            member_id=self._bot_user_id,
            everyone_id=guild_id,
            is_owner=is_owner,
        )

    async def channel_permissions(self, guild_id: int, channel_id: int) -> int | None:
        """Effective permissions for the bot in one channel, or ``None``."""
        state = await self._guild_state(guild_id)
        if state is None:
            return None
        overwrites = self._overwrites(channel_id)
        if overwrites is None:
            return None
        role_ids, role_perms, is_owner = state
        return effective_permissions(
            role_permissions=role_perms,
            role_ids=role_ids,
            member_id=self._bot_user_id,
            everyone_id=guild_id,
            overwrites=overwrites,
            is_owner=is_owner,
        )

    async def _guild_state(self, guild_id: int) -> tuple[frozenset[int], list[int], bool] | None:
        """The bot's role ids, those roles' permission bits, and ownership."""
        role_ids = await self._bot_role_ids(guild_id)
        if role_ids is None:
            return None
        try:
            guild = self._cache.get_guild(guild_id)
            roles = self._cache.get_roles_view_for_guild(guild_id)
        except Exception:  # pragma: no cover - defensive; the cache must not raise
            _log.debug("permission_probe_cache_error", guild_id=guild_id)
            return None
        if guild is None or not roles:
            return None
        perms = [int(role.permissions) for rid in role_ids if (role := roles.get(rid)) is not None]
        if not perms:
            return None
        return role_ids, perms, int(guild.owner_id) == self._bot_user_id

    async def _bot_role_ids(self, guild_id: int) -> frozenset[int] | None:
        """Role ids held by the bot in ``guild_id``, including ``@everyone``.

        Discord gives the ``@everyone`` role the guild's own id and omits it from
        a member's role list, so it is added explicitly -- without it the base
        permission set would be empty for a bot holding no extra roles.
        """
        cached = self._roles.get(guild_id)
        if cached is not None:
            return cached
        member = None
        try:
            member = self._cache.get_member(guild_id, self._bot_user_id)
        except Exception:  # pragma: no cover - defensive
            member = None
        if member is None:
            try:
                member = await self._rest.fetch_my_member(guild_id)
            except Exception:
                # No role information means no verdict; the preflight will fail
                # open and the action is attempted exactly as before.
                _log.debug("permission_probe_member_unavailable", guild_id=guild_id)
                return None
        role_ids = frozenset({int(r) for r in member.role_ids} | {guild_id})
        self._roles[guild_id] = role_ids
        return role_ids

    def _overwrites(self, channel_id: int) -> list[Overwrite] | None:
        """The channel's own overwrites, or ``None`` when it is not cached."""
        import hikari

        try:
            channel = self._cache.get_guild_channel(channel_id)
        except Exception:  # pragma: no cover - defensive
            return None
        if channel is None:
            return None
        return [
            Overwrite(
                target_id=int(ow.id),
                is_role=ow.type == hikari.PermissionOverwriteType.ROLE,
                allow=int(ow.allow),
                deny=int(ow.deny),
            )
            for ow in getattr(channel, "permission_overwrites", {}).values()
        ]
