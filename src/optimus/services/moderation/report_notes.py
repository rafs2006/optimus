"""The ``Action:`` line of a review card: what enforcement did, and why.

The report doubles as the guild's status feed, so this spells out the actual
outcome rather than the intended action: a failed step, a punishment the
privilege check changed, the spread rule that fired, and the cleanup that ran
alongside.
"""

from __future__ import annotations

from enum import StrEnum

from optimus.contracts.events import Action
from optimus.i18n import translate
from optimus.services.moderation.settlement import banned
from optimus.services.moderation.sweep import SweepOutcome
from optimus.shared.outcomes import ActionResult


class Boundary(StrEnum):
    """What the privilege check did to a punitive action, shown on the card."""

    #: The uploader had already left; banned by user id anyway.
    DEPARTED_BANNED = "departed_banned"
    #: The uploader is not a member because they are banned already.
    ALREADY_BANNED = "already_banned"
    #: The uploader had already left; timeout/kick cannot apply to a non-member.
    DEPARTED = "departed"
    #: The uploader's roles could not be read, so nothing punitive ran.
    UNVERIFIED = "unverified"


def action_note(
    action: Action,
    result: ActionResult,
    locale: str,
    *,
    boundary: Boundary | None = None,
    intended: Action | None = None,
    spread: int = 0,
    spread_minutes: int = 1,
    swept: SweepOutcome | None = None,
    cleared: int = 0,
) -> str:
    """Build the card's ``Action:`` text for one enforcement result."""
    note = action.value if result.success else f"{action.value} (failed: {result.detail})"
    if boundary is Boundary.DEPARTED_BANNED and not banned(result):
        # "banned by user ID" next to a failed ban told moderators the
        # opposite of what happened; the failure text says it alone.
        boundary = None
    if boundary is not None:
        # Say why the configured punishment did or did not happen, instead
        # of a bare "delete" that reads like the policy was ignored.
        why = translate(
            f"report.boundary_{boundary.value}", locale, action=(intended or action).value
        )
        note = f"{note} — {why}"
    if spread:
        # Say why a near match was acted on: the confidence on the card
        # is unchanged, so without this it reads like the bar was ignored.
        why = translate("report.spread_channels", locale, channels=spread, minutes=spread_minutes)
        note = f"{note} — {why}"
    if swept is not None and swept.touched:
        # Make the cross-channel cleanup visible to moderators: without it
        # the card reports one deletion while the sweep quietly removed a
        # campaign spanning a dozen channels.
        extra = f"purged {swept.deleted} more in {swept.channels} channels"
        if swept.failed:
            extra += f", {swept.failed} unreachable"
        if swept.harvested:
            extra += f", +{len(swept.harvested)} hashes blocklisted"
        note = f"{note} — {extra}"
    if cleared:
        note = f"{note} — cleared {cleared} other report(s) from this uploader"
    return note
