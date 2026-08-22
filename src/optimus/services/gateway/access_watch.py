"""Notice when the bot *gains* access to a channel, and clean up what it missed.

While the bot cannot see a channel it correctly does nothing: the preflight in
:mod:`optimus.services.moderation.permissions` resolves the answer from cache
and spends no requests. But that leaves a gap the reported incident made
obvious -- once a moderator fixes the overwrite, the scam images already sitting
in that channel stay up, because nothing tells the bot to look again. Only the
*next* upload gets handled.

This module closes that gap without polling and without persistence. Discord
already pushes the two edits that can grant access:

* ``GuildChannelUpdateEvent`` -- the channel's own overwrites changed;
* ``RoleUpdateEvent`` -- a role's guild-wide permissions changed.

Both events carry the **previous** state (``old_channel`` / ``old_role``), so a
transition can be computed locally by running
:func:`~optimus.services.moderation.permissions.effective_permissions` twice --
once over the old state and once over the new -- with no API call and nothing
remembered between events. Only a genuine blocked -> allowed transition triggers
a rescan, so renaming a channel or editing an unrelated permission costs
nothing.

Two deliberate limits, both consequences of not holding privileged intents:

* If ``old_channel``/``old_role`` is absent (cache miss) the transition is
  unknowable, so nothing is rescanned. Rescanning on every property change
  instead would mean re-reading history whenever anyone edits a topic.
* Granting access by *adding a role to the bot* is a ``MemberUpdateEvent``,
  which requires the privileged ``GUILD_MEMBERS`` intent this bot does not
  request. Editing the role, or the channel overwrite, both work.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol

from optimus.core.logging import get_logger
from optimus.services.moderation.permissions import (
    RESCAN_REQUIRES,
    Overwrite,
    effective_permissions,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence

    #: ``(guild_id, channel_ids) -> None``: rescan those channels' recent history.
    RescanFn = Callable[[int, "Sequence[int]"], Awaitable[None]]

_log = get_logger(__name__)


class RoleState(Protocol):
    """The bot's own role picture in a guild, as the permission probe knows it."""

    async def role_state(self, guild_id: int) -> tuple[frozenset[int], dict[int, int], bool] | None:
        """``(bot role ids, {role id: permission bits}, is_owner)`` or ``None``."""
        ...


def gained_access(
    *,
    role_ids: frozenset[int],
    role_permissions: dict[int, int],
    is_owner: bool,
    member_id: int,
    guild_id: int,
    before: Sequence[Overwrite],
    after: Sequence[Overwrite],
    required: int = RESCAN_REQUIRES,
) -> bool:
    """Whether ``required`` went from *not held* to *held* between two states.

    Pure: the caller supplies both overwrite sets, so this is exercised without
    a gateway. ``required`` defaults to what reading history needs.
    """

    def resolve(overwrites: Sequence[Overwrite]) -> int:
        return effective_permissions(
            role_permissions=role_permissions.values(),
            role_ids=role_ids,
            member_id=member_id,
            everyone_id=guild_id,
            overwrites=overwrites,
            is_owner=is_owner,
        )

    had = resolve(before)
    has = resolve(after)
    return bool(has & required == required) and not bool(had & required == required)


class AccessWatcher:
    """Turns permission edits into a debounced rescan of the affected channels.

    Moderators fix permissions in bursts -- three overwrites in ten seconds, or
    a role edit that unblocks nine channels at once. Each edit arrives as its
    own event, so the channels are collected and rescanned once after a short
    quiet period instead of once per event.
    """

    def __init__(
        self,
        state: RoleState,
        rescan: RescanFn,
        *,
        bot_user_id: int,
        debounce_seconds: float = 5.0,
    ) -> None:
        self._state = state
        self._rescan = rescan
        self._bot_user_id = bot_user_id
        self._debounce = debounce_seconds
        self._pending: dict[int, set[int]] = {}
        self._timer: asyncio.Task[None] | None = None

    async def channel_updated(
        self,
        guild_id: int,
        channel_id: int,
        *,
        before: Sequence[Overwrite] | None,
        after: Sequence[Overwrite],
    ) -> bool:
        """Handle a channel overwrite edit. Returns whether a rescan was queued.

        ``before`` is ``None`` when the previous state is not cached, in which
        case the transition is unknowable and nothing is queued.
        """
        if before is None:
            _log.debug("access_watch_no_prior_state", guild_id=guild_id, channel_id=channel_id)
            return False
        state = await self._state.role_state(guild_id)
        if state is None:
            return False
        role_ids, role_permissions, is_owner = state
        if not gained_access(
            role_ids=role_ids,
            role_permissions=role_permissions,
            is_owner=is_owner,
            member_id=self._bot_user_id,
            guild_id=guild_id,
            before=before,
            after=after,
        ):
            return False
        self._queue(guild_id, [channel_id])
        _log.info("access_regained_channel", guild_id=guild_id, channel_id=channel_id)
        return True

    async def role_updated(
        self,
        guild_id: int,
        role_id: int,
        *,
        before_permissions: int | None,
        after_permissions: int,
        channels: Iterable[tuple[int, Sequence[Overwrite]]],
    ) -> list[int]:
        """Handle a role permission edit. Returns the channels queued.

        A guild-wide role edit can unblock many channels at once, so each
        channel is evaluated with the old role bits and then the new ones; only
        the ones that actually flipped are queued. A role the bot does not hold
        cannot change the bot's own access and is ignored.
        """
        if before_permissions is None or before_permissions == after_permissions:
            return []
        state = await self._state.role_state(guild_id)
        if state is None:
            return []
        role_ids, role_permissions, is_owner = state
        if role_id not in role_ids:
            return []
        old_permissions = dict(role_permissions)
        old_permissions[role_id] = before_permissions
        new_permissions = dict(role_permissions)
        new_permissions[role_id] = after_permissions
        queued: list[int] = []
        for channel_id, overwrites in channels:
            before = effective_permissions(
                role_permissions=old_permissions.values(),
                role_ids=role_ids,
                member_id=self._bot_user_id,
                everyone_id=guild_id,
                overwrites=overwrites,
                is_owner=is_owner,
            )
            after = effective_permissions(
                role_permissions=new_permissions.values(),
                role_ids=role_ids,
                member_id=self._bot_user_id,
                everyone_id=guild_id,
                overwrites=overwrites,
                is_owner=is_owner,
            )
            if after & RESCAN_REQUIRES == RESCAN_REQUIRES and (
                before & RESCAN_REQUIRES != RESCAN_REQUIRES
            ):
                queued.append(channel_id)
        if queued:
            self._queue(guild_id, queued)
            _log.info(
                "access_regained_role",
                guild_id=guild_id,
                role_id=role_id,
                channels=len(queued),
            )
        return queued

    def _queue(self, guild_id: int, channel_ids: Iterable[int]) -> None:
        self._pending.setdefault(guild_id, set()).update(channel_ids)
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(self._debounce)
        await self.flush()

    async def flush(self) -> None:
        """Rescan everything queued so far. Safe to call directly in tests."""
        pending, self._pending = self._pending, {}
        for guild_id, channel_ids in pending.items():
            try:
                await self._rescan(guild_id, sorted(channel_ids))
            except Exception:  # pragma: no cover - a rescan must not kill the listener
                _log.warning("access_rescan_failed", guild_id=guild_id, exc_info=True)

    async def aclose(self) -> None:
        """Cancel a pending debounce timer during shutdown."""
        timer = self._timer
        self._timer = None
        if timer is not None and not timer.done():
            timer.cancel()
            # Shutdown must not be blocked by a rescan that was mid-flight.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await timer
