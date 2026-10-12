"""When an uploader counts as dealt with, and what then happens to their other posts.

An uploader is *settled* once the bot handled one of their posts in full or a
moderator confirmed one. While they are settled, every other flagged image of
theirs is deleted without a card of its own: the scam was already found and
the account banned, so a moderator has nothing left to decide about them.
"""

from __future__ import annotations

from optimus.contracts.events import Action, Verdict, VerdictEvent
from optimus.core.logging import get_logger
from optimus.services.moderation.policy import Decision
from optimus.shared.failures import FailureKind
from optimus.shared.outcomes import ActionResult, Step

_log = get_logger(__name__)


def fully_handled(
    event: VerdictEvent,
    configured_action: Action,
    decision: Decision,
    action: Action,
    result: ActionResult,
) -> bool:
    """Whether the bot finished the job itself, so no moderator is needed.

    True only for a match against this server's own blocklist that ran the
    server's configured action in full: the post is gone and every punitive
    step succeeded (a ban of an uploader who already left counts). Anything a
    person should look at keeps an open card: a global-only match, a near
    match queued for review, safe mode, a refused or downgraded punishment, a
    missing permission, a member report.
    """
    if event.confirmed_by is not None or event.reported_by is not None:
        return False
    if decision is not Decision.AUTO_ACT or action in (Action.NONE, Action.REPORT_ONLY):
        return False
    if event.matched_source != "guild" or not event.matched_hash_id:
        return False
    if action is not configured_action:
        return False
    return result.success and bool(result.steps) and result.message_deleted


def flagged(event: VerdictEvent) -> bool:
    """Whether anything pointed at this image: a match, the risk scan, a member."""
    return event.reported_by is not None or event.verdict in (Verdict.SCAM, Verdict.AMBIGUOUS)


def followup_path(event: VerdictEvent, *, safe_mode: bool, settled: bool) -> str | None:
    """Why this image of a known campaign is deleted quietly; ``None`` to take the normal path.

    ``"settled"``: the uploader was banned or confirmed moments ago, so any
    flagged image goes -- a near or global match, a risk-scan finding without
    a hash match, a member report. ``"campaign"``: a confirmed campaign's
    repost that matches this server's own blocklist. Safe mode never deletes.
    """
    if safe_mode:
        return None
    if settled:
        return "settled" if flagged(event) else None
    if event.matched_hash_id and event.matched_source == "guild":
        return "campaign"
    return None


def log_followup(event: VerdictEvent, path: str) -> None:
    """One line per post deleted because its uploader was already dealt with.

    These deletions skip ``verdict_decided`` (they never reach the policy),
    so without this line the log could not show where a post went.
    """
    _log.info(
        "followup_removed",
        guild_id=event.guild_id,
        message_id=event.message_id,
        uploader_id=event.uploader_id,
        verdict=event.verdict.value,
        matched=bool(event.matched_hash_id),
        reported=event.reported_by is not None,
        path=path,
    )


def banned(result: ActionResult) -> bool:
    """Whether the ban step of ``result`` actually succeeded."""
    return any(s.step is Step.BAN and s.success for s in result.steps)


def rate_limited(result: ActionResult) -> bool:
    """Whether ``result`` failed only for want of rate budget (ours or Discord's)."""
    if result.success:
        return False
    if result.detail == "rate_limited":
        return True
    return any(
        s.failure is not None and s.failure.kind is FailureKind.RATE_LIMITED
        for s in result.failed_steps
    )
