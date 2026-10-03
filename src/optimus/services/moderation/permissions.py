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


class ChannelInventory(Protocol):
    """Resolves the bot's permissions across every channel of a guild at once.

    Separate from :class:`PermissionProbe` so the per-action preflight path
    keeps its two-method surface: only the ``/config permissions`` audit needs
    a whole-guild view.
    """

    async def channel_access(self, guild_id: int) -> list[tuple[int, int]] | None:
        """``(channel_id, permission bits)`` per channel, or ``None`` if unknown."""
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


@dataclass(frozen=True, slots=True)
class AccessReport:
    """What the bot can and cannot do across a whole guild, right now.

    Built from cached permissions for every textable channel, so ``/config
    permissions`` can answer "where are we blind?" without a request per
    channel -- and, unlike a record of past failures, it also names channels no
    scam has landed in yet.

    Channels are sorted into what actually needs a moderator's attention:

    * the **review channel**, checked on its own, because it is the one private
      channel the bot must see -- without it no card reaches a moderator;
    * **hidden** channels (the bot lacks View Channel): treated as private by
      design and only counted, never flagged. Staff, beta and archive channels
      hide themselves from everyone; demanding access to them would push a
      server to hand the bot rooms it has no business in;
    * **blocked** channels: visible, but enforcement under the current action
      policy is impossible there (it cannot delete);
    * **advisory** channels: visible and watched, and only short of what a
      deleting policy would need -- a heads-up while on ``report_only``.
    """

    #: Visible channels evaluated (excludes ignored and hidden ones).
    checked: int
    #: Channels skipped because the guild's ignore list contains them.
    ignored: int
    #: ``(channel_id, missing permission names)`` where the current policy
    #: cannot be carried out.
    blocked: tuple[tuple[int, tuple[str, ...]], ...]
    #: Guild-wide permissions the punitive step needs but the bot lacks.
    guild_missing: tuple[str, ...]
    #: Channels the bot cannot see; not monitored, not a fault.
    hidden: int = 0
    #: ``(channel_id, missing)`` the bot watches but could not delete in, were
    #: deleting switched on. Only populated under ``report_only``.
    advisory: tuple[tuple[int, tuple[str, ...]], ...] = ()
    #: The linked review channel, or ``None`` when ``/setup`` has not run.
    review_channel_id: int | None = None
    #: What the bot lacks to post review cards there (empty = fine).
    review_missing: tuple[str, ...] = ()
    #: The linked review channel no longer exists in the guild.
    review_channel_missing: bool = False

    @property
    def review_ok(self) -> bool:
        """Whether review cards can be posted (fails closed if none is linked)."""
        return (
            self.review_channel_id is not None
            and not self.review_channel_missing
            and not self.review_missing
        )

    @property
    def ok(self) -> bool:
        """Whether nothing needs fixing. Hidden and advisory channels never count."""
        return self.review_ok and not self.blocked and not self.guild_missing

    def grouped(self) -> tuple[tuple[tuple[str, ...], tuple[int, ...]], ...]:
        """Blocked channels bucketed by identical missing-permission sets.

        Ten channels missing the same permission render as one line, not ten.
        """
        return _group(self.blocked)

    def advisory_grouped(self) -> tuple[tuple[tuple[str, ...], tuple[int, ...]], ...]:
        return _group(self.advisory)


def _group(
    rows: tuple[tuple[int, tuple[str, ...]], ...],
) -> tuple[tuple[tuple[str, ...], tuple[int, ...]], ...]:
    buckets: dict[tuple[str, ...], list[int]] = {}
    for channel_id, missing in rows:
        buckets.setdefault(missing, []).append(channel_id)
    return tuple(
        (missing, tuple(channel_ids))
        for missing, channel_ids in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    )


def _is_hidden(granted: int | None) -> bool:
    """The bot cannot see this channel (unknown permissions fail open: visible)."""
    if granted is None or granted & ADMINISTRATOR:
        return False
    return not granted & VIEW_CHANNEL


def build_access_report(
    channel_access: Sequence[tuple[int, int]],
    *,
    ignored_channels: frozenset[int] = frozenset(),
    guild_permissions: int | None = None,
    punitive: int = 0,
    deletes: bool = True,
    review_channel_id: int | None = None,
) -> AccessReport:
    """Summarize per-channel access into a report for display.

    ``deletes`` is whether the guild's action policy removes scam images: only
    then is Manage Messages a requirement. Under ``report_only`` the bot needs
    nothing beyond seeing a channel, and a missing Manage Messages is reported
    as advisory. ``punitive`` is the guild-wide bit the ban/kick/timeout step
    needs, checked once rather than per channel. The review channel is checked
    against what posting a card needs, whether or not it is otherwise hidden.
    """
    blocked: list[tuple[int, tuple[str, ...]]] = []
    advisory: list[tuple[int, tuple[str, ...]]] = []
    checked = ignored = hidden = 0
    review_granted: int | None = None
    review_seen = False
    for channel_id, granted in channel_access:
        if channel_id == review_channel_id:
            review_seen = True
            review_granted = granted
            continue
        if channel_id in ignored_channels:
            ignored += 1
            continue
        if _is_hidden(granted):
            hidden += 1
            continue
        checked += 1
        result = check(DELETE_REQUIRES, granted)
        if result.ok:
            continue
        (blocked if deletes else advisory).append((channel_id, result.missing))

    review_missing: tuple[str, ...] = ()
    if review_seen:
        review_result = check(REPORT_REQUIRES, review_granted)
        review_missing = () if review_result.ok else review_result.missing

    guild_missing: tuple[str, ...] = ()
    if punitive and guild_permissions is not None:
        guild_result = check(punitive, guild_permissions)
        if not guild_result.ok:
            guild_missing = guild_result.missing
    return AccessReport(
        checked=checked,
        ignored=ignored,
        blocked=tuple(blocked),
        guild_missing=guild_missing,
        hidden=hidden,
        advisory=tuple(advisory),
        review_channel_id=review_channel_id,
        review_missing=review_missing,
        review_channel_missing=review_channel_id is not None and not review_seen,
    )


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
