"""When an uploader counts as dealt with, and what then happens to their other posts.

An uploader is *settled* once the bot handled one of their posts in full or a
moderator confirmed one, under whatever action policy the server uses. While
they are settled, their other hash matches that would ask a moderator (near
or global) are deleted without a card of their own. If the settlement also
banned them, the same goes for an image only the risk scan flagged and for a
member report: the scam was found and the account is gone, so a moderator has
nothing left to decide. Below the server's ``mod_queue_threshold`` nothing is
touched -- such an image would not have reached a moderator either.
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


def flagged(event: VerdictEvent, queue_threshold: float) -> bool:
    """Whether this image would ask a moderator: a member report, or a scam or
    suspicious verdict at or above the server's ``mod_queue_threshold``."""
    if event.reported_by is not None:
        return True
    suspect = event.verdict in (Verdict.SCAM, Verdict.AMBIGUOUS)
    return suspect and event.confidence >= queue_threshold


def followup_path(
    event: VerdictEvent,
    *,
    safe_mode: bool,
    settled: bool,
    banned: bool,
    queue_threshold: float,
) -> str | None:
    """Why this image of a known campaign is deleted quietly; ``None`` to take the normal path.

    ``"settled"``: the uploader was handled or confirmed moments ago and this
    is another hash match of theirs (a near or global match). ``"banned"``:
    that settlement also banned them, so an image only the risk scan flagged
    or a member report goes too. Both need :func:`flagged`. ``"campaign"``: a
    confirmed campaign's repost that matches this server's own blocklist.
    Safe mode never deletes.
    """
    if safe_mode:
        return None
    if settled and flagged(event, queue_threshold):
        if event.matched_hash_id:
            return "settled"
        if banned:
            return "banned"
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
