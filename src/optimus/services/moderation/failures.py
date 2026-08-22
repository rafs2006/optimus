"""Classification of Discord enforcement failures into actionable causes.

Enforcement fails for many reasons and they are not equivalent: a missing
``View Channel`` overwrite is an admin's five-second fix, an already-deleted
message means the goal is *met*, and a 500 from Discord means "try later".
Historically all of these collapsed into ``error:ForbiddenError``, which told a
moderator nothing and made a permission gap look like a bot bug -- a real
incident cost several debugging rounds for exactly this reason.

Every failure is mapped to a :class:`Failure` carrying three decisions:

* :attr:`Failure.satisfied` -- the step's goal is already true, so callers must
  treat it as success (an already-deleted message, an already-banned user).
* :attr:`Failure.recoverable` -- the same call could succeed later once
  something outside the bot changes (a permission grant, Discord recovering).
  Callers use this to decide whether to remember the channel and retry after a
  permission change, rather than retrying blindly.
* :attr:`Failure.message_key` -- an i18n key naming the exact fix, so the
  report card and command reply can tell an admin what to change.

Classification is duck-typed on ``code``/``status`` rather than isinstance
checks against hikari, so this module stays unit-testable with plain fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from optimus.core.circuit import CircuitOpenError


class FailureKind(StrEnum):
    """A classified enforcement failure cause."""

    #: No ``View Channel`` for the bot in this channel (or its category).
    MISSING_ACCESS = "missing_access"
    #: Channel or guild permission missing, or the bot's role is too low.
    MISSING_PERMISSION = "missing_permission"
    #: The guild requires 2FA for moderation and the app owner has none.
    MFA_REQUIRED = "mfa_required"
    #: The message is already gone -- the delete's goal is met.
    ALREADY_GONE = "already_gone"
    #: The channel no longer exists (stale configuration).
    UNKNOWN_CHANNEL = "unknown_channel"
    #: The member already left the guild.
    MEMBER_GONE = "member_gone"
    #: The user is already banned -- the ban's goal is met.
    ALREADY_BANNED = "already_banned"
    #: The target is not a channel/message this action can apply to.
    UNSUPPORTED_TARGET = "unsupported_target"
    #: Discord system messages cannot be deleted.
    SYSTEM_MESSAGE = "system_message"
    #: The guild hit Discord's cap on bans for non-members.
    BAN_LIMIT = "ban_limit"
    #: The user's DMs are closed -- never an enforcement failure.
    DM_BLOCKED = "dm_blocked"
    #: Rate limited by Discord.
    RATE_LIMITED = "rate_limited"
    #: The local circuit breaker is open (Discord unhealthy).
    CIRCUIT_OPEN = "circuit_open"
    #: A duplicate/redelivered verdict already enforced.
    DUPLICATE = "duplicate"
    #: Discord server-side error.
    TRANSIENT = "transient"
    #: Anything unrecognized -- treated as a real failure, reported verbatim.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Failure:
    """A classified failure with the decisions callers need."""

    kind: FailureKind
    #: Discord JSON error code when known (0 otherwise).
    code: int = 0
    #: Exception type name, for logs and audit detail.
    exception: str = ""

    @property
    def satisfied(self) -> bool:
        """Whether the step's goal is already true, so this counts as success."""
        return self.kind in _SATISFIED

    @property
    def recoverable(self) -> bool:
        """Whether this same call could succeed later without code changes."""
        return self.kind in _RECOVERABLE

    @property
    def permission_related(self) -> bool:
        """Whether an admin can fix this by changing Discord permissions."""
        return self.kind in _PERMISSION_RELATED

    @property
    def message_key(self) -> str:
        """i18n key for the admin-facing explanation of this failure."""
        return f"failure.{self.kind.value}"

    @property
    def detail(self) -> str:
        """Compact audit detail, e.g. ``missing_access:50001``.

        Unrecognized failures carry the exception name instead, since that is
        the only diagnostic left when neither a Discord code nor a status
        matched.
        """
        if self.code:
            return f"{self.kind.value}:{self.code}"
        if self.kind is FailureKind.UNKNOWN and self.exception:
            return f"{self.kind.value}:{self.exception}"
        return self.kind.value


#: Goal already met -- callers must not treat these as failures.
_SATISFIED = frozenset(
    {
        FailureKind.ALREADY_GONE,
        FailureKind.ALREADY_BANNED,
    }
)

#: Could succeed later once permissions change or Discord recovers. These are
#: the only kinds worth remembering for a post-permission-change retry.
_RECOVERABLE = frozenset(
    {
        FailureKind.MISSING_ACCESS,
        FailureKind.MISSING_PERMISSION,
        FailureKind.MFA_REQUIRED,
        FailureKind.RATE_LIMITED,
        FailureKind.CIRCUIT_OPEN,
        FailureKind.TRANSIENT,
    }
)

#: Fixable by an admin editing Discord permissions.
_PERMISSION_RELATED = frozenset(
    {
        FailureKind.MISSING_ACCESS,
        FailureKind.MISSING_PERMISSION,
        FailureKind.MFA_REQUIRED,
    }
)

#: Discord JSON error code -> classified kind.
#: https://discord.com/developers/docs/topics/opcodes-and-status-codes
_BY_CODE: dict[int, FailureKind] = {
    10003: FailureKind.UNKNOWN_CHANNEL,
    10007: FailureKind.MEMBER_GONE,
    10008: FailureKind.ALREADY_GONE,
    10013: FailureKind.MEMBER_GONE,
    10026: FailureKind.ALREADY_GONE,  # Unknown ban -- nothing to undo.
    30035: FailureKind.BAN_LIMIT,
    40007: FailureKind.ALREADY_BANNED,
    50001: FailureKind.MISSING_ACCESS,
    50007: FailureKind.DM_BLOCKED,
    50013: FailureKind.MISSING_PERMISSION,
    50021: FailureKind.SYSTEM_MESSAGE,
    50024: FailureKind.UNSUPPORTED_TARGET,
    60003: FailureKind.MFA_REQUIRED,
}

#: HTTP status -> kind, used only when the JSON error code is absent/unmapped.
_BY_STATUS: dict[int, FailureKind] = {
    403: FailureKind.MISSING_PERMISSION,
    404: FailureKind.ALREADY_GONE,
    429: FailureKind.RATE_LIMITED,
}


def _int_attr(exc: BaseException, name: str) -> int:
    value = getattr(exc, name, None)
    return value if isinstance(value, int) else 0


def classify(exc: BaseException) -> Failure:
    """Classify ``exc`` into an actionable :class:`Failure`.

    Prefers Discord's JSON error code, which is far more specific than the HTTP
    status: 403 alone cannot distinguish "cannot see the channel" (``50001``,
    fix the overwrite) from "cannot delete messages here" (``50013``, grant
    Manage Messages), and reporting only the status is what made a permission
    gap indistinguishable from a bug.
    """
    exception = type(exc).__name__
    if isinstance(exc, CircuitOpenError):
        return Failure(FailureKind.CIRCUIT_OPEN, exception=exception)

    code = _int_attr(exc, "code")
    kind = _BY_CODE.get(code)
    if kind is not None:
        return Failure(kind, code=code, exception=exception)

    # Every hikari HTTP error carries ``status`` (and ``code``, 0 when Discord
    # sent none), so the status fallback covers the real client without any
    # isinstance coupling to hikari's exception hierarchy.
    status = _int_attr(exc, "status")
    if status >= 500:
        return Failure(FailureKind.TRANSIENT, code=code, exception=exception)
    kind = _BY_STATUS.get(status)
    if kind is not None:
        return Failure(kind, code=code, exception=exception)
    return Failure(FailureKind.UNKNOWN, code=code, exception=exception)
