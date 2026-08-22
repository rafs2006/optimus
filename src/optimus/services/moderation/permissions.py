"""Preflight checks for the permissions an enforcement action actually needs.

Discord answers a doomed call with ``403``, which costs a request, burns rate
limit and -- during a raid in a channel the bot cannot see -- produces one
failed request per scam image. Worse, the refusal used to reach the moderator as
an opaque error, so a five-second permission fix looked like a bot bug.

This module computes what an action requires and compares it against the bot's
*effective* permissions, so the caller can skip a call that cannot succeed and
name the exact missing permission instead.

Two deliberate properties:

* **Fails open.** When permissions cannot be resolved (cache miss, unknown
  channel) the preflight returns :attr:`PreflightResult.ok`, so a stale cache
  can never silently stop enforcement. A real ``403`` is still classified by
  :mod:`optimus.services.moderation.failures`.
* **Pure and hikari-free.** Bit values are declared locally (asserted against
  hikari in tests) so this logic is unit-testable without a live gateway.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from optimus.contracts.events import Action
from optimus.services.moderation.failures import Failure, FailureKind

#: Discord permission bits. Values are pinned by a test against
#: ``hikari.Permissions`` so a hikari change cannot silently skew a preflight.
ADMINISTRATOR = 1 << 3
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
MANAGE_MESSAGES = 1 << 13
EMBED_LINKS = 1 << 14
READ_MESSAGE_HISTORY = 1 << 16
BAN_MEMBERS = 1 << 2
KICK_MEMBERS = 1 << 1
MODERATE_MEMBERS = 1 << 40

#: Human-readable names, used verbatim in the message shown to admins so it
#: matches the label in Discord's own permission UI.
PERMISSION_NAMES: dict[int, str] = {
    ADMINISTRATOR: "Administrator",
    VIEW_CHANNEL: "View Channel",
    SEND_MESSAGES: "Send Messages",
    MANAGE_MESSAGES: "Manage Messages",
    EMBED_LINKS: "Embed Links",
    READ_MESSAGE_HISTORY: "Read Message History",
    BAN_MEMBERS: "Ban Members",
    KICK_MEMBERS: "Kick Members",
    MODERATE_MEMBERS: "Timeout Members",
}

#: Every bit set -- what an administrator or guild owner effectively holds.
ALL_PERMISSIONS = (1 << 64) - 1

#: Deleting someone else's message in a channel.
DELETE_REQUIRES = VIEW_CHANNEL | MANAGE_MESSAGES
#: Posting a review card (an embed) into the review channel.
REPORT_REQUIRES = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS
#: Reading a channel's history for a rescan.
RESCAN_REQUIRES = VIEW_CHANNEL | READ_MESSAGE_HISTORY

#: Guild-level permission each punitive action needs.
_PUNITIVE_REQUIRES: dict[Action, int] = {
    Action.DELETE_BAN: BAN_MEMBERS,
    Action.DELETE_KICK: KICK_MEMBERS,
    Action.DELETE_TIMEOUT: MODERATE_MEMBERS,
}


class PermissionProbe(Protocol):
    """Resolves the bot's effective permissions, ideally from cache.

    Implementations must return ``None`` rather than raising when the answer is
    unknown, so the caller can fail open instead of blocking enforcement.
    """

    async def channel_permissions(self, guild_id: int, channel_id: int) -> int | None:
        """Effective permission bits for the bot in one channel."""
        ...

    async def guild_permissions(self, guild_id: int) -> int | None:
        """Guild-wide permission bits for the bot (no channel overwrites)."""
        ...


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Whether a call can succeed, and what is missing when it cannot."""

    ok: bool
    #: Missing permission names, in Discord's own wording.
    missing: tuple[str, ...] = ()
    #: Classified failure to record/report when ``ok`` is False.
    failure: Failure | None = None

    @property
    def missing_text(self) -> str:
        """Missing permissions as a comma-separated list for display."""
        return ", ".join(self.missing)


def missing_names(required: int, granted: int) -> tuple[str, ...]:
    """Names of the bits in ``required`` that ``granted`` lacks."""
    return tuple(
        name for bit, name in PERMISSION_NAMES.items() if required & bit and not granted & bit
    )


def check(required: int, granted: int | None) -> PreflightResult:
    """Compare ``required`` against ``granted``, failing open on ``None``.

    ``ADMINISTRATOR`` short-circuits exactly as Discord does.
    """
    if granted is None:
        return PreflightResult(ok=True)
    if granted & ADMINISTRATOR:
        return PreflightResult(ok=True)
    missing = missing_names(required, granted)
    if not missing:
        return PreflightResult(ok=True)
    # A denied VIEW_CHANNEL is Discord's "Missing Access" (50001); anything
    # else is a specific permission gap (50013). Distinguishing them matters:
    # the fixes live in different parts of Discord's UI.
    kind = (
        FailureKind.MISSING_ACCESS
        if not granted & VIEW_CHANNEL and required & VIEW_CHANNEL
        else FailureKind.MISSING_PERMISSION
    )
    return PreflightResult(ok=False, missing=missing, failure=Failure(kind))


@dataclass(frozen=True, slots=True)
class Overwrite:
    """One channel permission overwrite, for a role or a single member."""

    target_id: int
    is_role: bool
    allow: int
    deny: int


def effective_permissions(
    *,
    role_permissions: Iterable[int],
    role_ids: frozenset[int],
    member_id: int,
    everyone_id: int,
    overwrites: Sequence[Overwrite] = (),
    is_owner: bool = False,
) -> int:
    """Compute a member's effective permissions in a channel.

    Implements Discord's documented precedence exactly: guild-wide role
    permissions, then the ``@everyone`` overwrite, then all role overwrites
    (every deny before every allow), then the member-specific overwrite. Owner
    and administrator both bypass overwrites entirely -- which is why the bot's
    role card can show every permission granted while a category overwrite
    still blocks it in one channel.

    ``everyone_id`` is the guild id, since Discord gives the ``@everyone`` role
    the guild's own snowflake.
    """
    if is_owner:
        return ALL_PERMISSIONS
    base = 0
    for value in role_permissions:
        base |= value
    if base & ADMINISTRATOR:
        return ALL_PERMISSIONS

    role_allow = 0
    role_deny = 0
    member_allow = 0
    member_deny = 0
    for ow in overwrites:
        if ow.is_role and ow.target_id == everyone_id:
            base &= ~ow.deny
            base |= ow.allow
        elif ow.is_role and ow.target_id in role_ids:
            role_deny |= ow.deny
            role_allow |= ow.allow
        elif not ow.is_role and ow.target_id == member_id:
            member_deny |= ow.deny
            member_allow |= ow.allow
    base &= ~role_deny
    base |= role_allow
    base &= ~member_deny
    base |= member_allow
    return base


def punitive_requirement(action: Action) -> int:
    """Guild permission bits ``action``'s punitive step needs (0 if none)."""
    return _PUNITIVE_REQUIRES.get(action, 0)


async def preflight_delete(
    probe: PermissionProbe, guild_id: int, channel_id: int
) -> PreflightResult:
    """Whether the bot can delete a message in ``channel_id``."""
    return check(DELETE_REQUIRES, await probe.channel_permissions(guild_id, channel_id))


async def preflight_report(
    probe: PermissionProbe, guild_id: int, channel_id: int
) -> PreflightResult:
    """Whether the bot can post a review card into ``channel_id``."""
    return check(REPORT_REQUIRES, await probe.channel_permissions(guild_id, channel_id))


async def preflight_punitive(
    probe: PermissionProbe, guild_id: int, action: Action
) -> PreflightResult:
    """Whether the bot can apply ``action``'s punitive step in this guild."""
    required = punitive_requirement(action)
    if not required:
        return PreflightResult(ok=True)
    return check(required, await probe.guild_permissions(guild_id))
