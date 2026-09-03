"""Turning enforcement failures into instructions a moderator can act on.

A refused action used to reach the server as ``error:ForbiddenError`` in a log
line nobody reads, so a missing channel overwrite was indistinguishable from a
bot bug. Everything here exists to make the opposite true: whenever the bot
cannot finish an action, the review card says which step failed, why, and what
to change in Discord.

The wording lives in the i18n catalog (:mod:`optimus.i18n`), so the explanation
is localized like every other user-facing string.
"""

from __future__ import annotations

from optimus.i18n import translate
from optimus.services.moderation.actions import ActionResult, Step, StepOutcome
from optimus.services.moderation.failures import FailureKind
from optimus.services.moderation.permissions import AccessReport, PreflightResult

#: Blocked channels listed per group before collapsing into "and N more", so a
#: 200-channel server cannot produce a reply past Discord's length limit.
_MAX_CHANNELS_PER_GROUP = 15

#: Per-step description of what could not be done ("Could not delete...").
_STEP_KEY = {
    Step.DELETE: "failure.step_delete",
    Step.TIMEOUT: "failure.step_timeout",
    Step.KICK: "failure.step_kick",
    Step.BAN: "failure.step_ban",
    Step.DM: "failure.step_dm",
}


#: Kinds that have a ``_named`` catalog variant quoting the missing permissions.
_NAMED_KINDS = frozenset({FailureKind.MISSING_ACCESS, FailureKind.MISSING_PERMISSION})


def _reason_key(kind: FailureKind, missing: tuple[str, ...]) -> str:
    """Catalog key for a reason, preferring the variant that names permissions.

    Only two kinds have a ``_named`` variant, and asking for one that does not
    exist would surface the raw key to a moderator, so membership is checked
    rather than assumed from ``missing`` being non-empty.
    """
    if missing and kind in _NAMED_KINDS:
        return f"failure.{kind.value}_named"
    return f"failure.{kind.value}"


def explain_step(outcome: StepOutcome, locale: str, *, channel_id: int | None = None) -> str:
    """Render one failed step as ``<what failed>: <why, and the fix>``.

    When a preflight identified the exact missing permissions, a ``_named``
    variant of the reason is used so the message can quote Discord's own
    permission labels instead of guessing.
    """
    what = translate(_STEP_KEY[outcome.step], locale)
    kind = outcome.failure.kind if outcome.failure else FailureKind.UNKNOWN
    key = _reason_key(kind, outcome.missing)
    why = translate(
        key,
        locale,
        channel=f"<#{channel_id}>" if channel_id else "",
        missing=", ".join(outcome.missing),
    )
    return f"{what}: {why}"


def explain_result(
    result: ActionResult, locale: str, *, channel_id: int | None = None
) -> str | None:
    """Render every failed step of ``result``, or ``None`` when all succeeded.

    Channel-scoped steps get the channel mention so an admin can click straight
    through to the overwrite that needs changing; guild-scoped steps (ban, kick,
    timeout) deliberately omit it -- naming a channel there would send someone
    to the wrong settings page.
    """
    failed = result.failed_steps
    if not failed:
        return None
    lines = [
        explain_step(step, locale, channel_id=channel_id if step.step is Step.DELETE else None)
        for step in failed
    ]
    return "\n".join(lines)


def explain_access_report(report: AccessReport, locale: str) -> str:
    """Render an :class:`AccessReport` as the body of ``/config permissions``.

    Deliberately silent when healthy -- a wall of green checkmarks for every
    channel is noise. When something is blocked, channels are grouped by the
    permission they are missing and rendered as mentions, which Discord shows as
    the channel's real name rather than a raw id.
    """
    if report.ok:
        return translate("command.permissions_all_ok", locale, checked=report.checked)
    lines = [
        translate(
            "command.permissions_header",
            locale,
            blocked=len(report.blocked),
            checked=report.checked,
        )
    ]
    if report.guild_missing:
        lines.append(
            "\n"
            + translate(
                "command.permissions_guild_gap",
                locale,
                missing=", ".join(report.guild_missing),
            )
        )
    for missing, channel_ids in report.grouped():
        shown = channel_ids[:_MAX_CHANNELS_PER_GROUP]
        listing = "  ".join(f"<#{cid}>" for cid in shown)
        hidden = len(channel_ids) - len(shown)
        if hidden:
            listing += " " + translate("command.permissions_more", locale, count=hidden)
        lines.append(
            "\n"
            + translate("command.permissions_group", locale, missing=", ".join(missing))
            + "\n"
            + listing
        )
    lines.append("\n" + translate("command.permissions_how_to_fix", locale))
    if report.ignored:
        lines.append(translate("command.permissions_ignored", locale, count=report.ignored))
    return "\n".join(lines)


def explain_rescan_summary(channel_ids: tuple[int, ...], messages: int, locale: str) -> str:
    """One line for the review channel after an access-regained rescan.

    Posted so a moderator who has just fixed a permission sees that the bot
    noticed and went back over what it missed, rather than having to guess
    whether the fix took effect.
    """
    return translate(
        "command.access_regained",
        locale,
        channels="  ".join(f"<#{cid}>" for cid in channel_ids),
        messages=messages,
    )


def explain_setup_replay_summary(shown: int, more: int, days: int, locale: str) -> str:
    """One line posted to the review channel right after a ``/setup`` replay.

    Announces how many pre-existing detections were re-posted, and -- when the
    cap kicked in -- how many older ones were left out. The count is dominated
    by the 3-day / 50-newest bound in the caller; without a summary line the
    burst of cards would look like a live incident wave rather than a
    one-time catch-up.
    """
    truncated = translate("command.setup_replay_truncated", locale, more=more) if more > 0 else ""
    return translate(
        "command.setup_replay_summary",
        locale,
        shown=shown,
        days=days,
        truncated=truncated,
    )


def explain_preflight(
    result: PreflightResult, locale: str, *, channel_id: int | None = None
) -> str | None:
    """Render a refused preflight as a reason, or ``None`` when it passed.

    Used before any request is made -- so a moderator who submits a review in a
    channel the bot cannot touch is told immediately, instead of being told the
    submission succeeded and left to discover later that nothing happened.
    """
    if result.ok or result.failure is None:
        return None
    return translate(
        _reason_key(result.failure.kind, result.missing),
        locale,
        channel=f"<#{channel_id}>" if channel_id else "",
        missing=result.missing_text,
    )
