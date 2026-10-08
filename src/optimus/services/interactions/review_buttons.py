"""Review-card buttons: Confirm scam, False positive, Dismiss, Ban, Unban, Whitelist.

Split out of :mod:`optimus.services.interactions.handlers` (behaviour
unchanged). :func:`handle_review_button` re-checks the clicker's permission,
resolves the card, then hands off to one function per button; each one is a
straight read of what that button does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from optimus.core.logging import get_logger
from optimus.db.models import GuildHash, GuildWhitelist
from optimus.services.interactions.attachment_hash import AttachmentHashError
from optimus.services.interactions.handlers import (
    DetectionFacts,
    ImageHashes,
    InteractionContext,
    InteractionDeps,
    InteractionResponse,
    _entry_refs,
    _lift_whitelist,
    _require,
    review_action_permission,
)
from optimus.services.interactions.logic import (
    CommandError,
    InteractionRejected,
    Permission,
    has_permission,
)
from optimus.shared import reasons
from optimus.shared.review import BUTTON_LABELS, ParsedCustomId, ReviewAction

_log = get_logger(__name__)


def _card_note(
    action: ReviewAction,
    user_id: int,
    *,
    key: str = "card.handled",
    whitelisted: Sequence[GuildWhitelist] = (),
) -> dict[str, Any]:
    """The ``card_note_*`` kwargs marking a card as handled by ``user_id``.

    ``whitelisted`` are the whitelist entries the decision created: the card
    names their numbers, so a misclick can be undone with
    ``/scamhash unwhitelist``.
    """
    params: dict[str, Any] = {"action": BUTTON_LABELS[action], "user_id": user_id}
    if whitelisted:
        key = f"{key}_whitelisted"
        params["count"] = len(whitelisted)
        params["entries"] = _entry_refs(whitelisted)
    return {"card_note_key": key, "card_note_params": params}


async def _is_global_participant(deps: InteractionDeps, guild_id: int) -> bool:
    """Whether this server's verdicts may touch the shared scam list.

    Both halves are required, and the same pair gates voting an entry *in* and
    revoking one *out*: the server opted in (``optin_global_db``) and the bot
    owner approved it for contribution. Without the check on revocation, any
    server -- including one a scammer set up for the purpose -- could post
    their own image, report it, press False positive, and pull it off the list
    for every server at once.
    """
    config = await deps.get_config(guild_id)
    if not bool(config.get("optin_global_db", False)):
        return False
    return await deps.is_trusted_guild(guild_id)


async def _resolve_image_hashes(deps: InteractionDeps, det: DetectionFacts) -> ImageHashes | None:
    """Resolve the hash ensemble for the image behind ``det``.

    Prefers the hashes persisted at detection time; falls back to re-fetching
    the attachment (member reports never hash up front). Both the REST lookup
    and the fetch+decode happen *before* the caller's first DB write on
    purpose: SQLite holds the write lock from the first INSERT/UPDATE to
    commit, and network work inside that window starves concurrent writers
    (see :meth:`InteractionDeps.compute_attachment_hashes`).
    """
    stored = det.hashes
    if stored is not None:
        try:
            return ImageHashes(
                phash=int(stored["phash"]),
                dhash=int(stored["dhash"]),
                whash=int(stored["whash"]),
                ahash=int(stored["ahash"]),
            )
        except (KeyError, TypeError, ValueError):  # pragma: no cover - corrupt row
            _log.warning("detection_hashes_corrupt", detection_id=det.detection_id)
    url = await deps.rest_attachment_url(det.channel_id, det.message_id, det.attachment_id)
    if url is None:
        return None
    try:
        computed = await deps.compute_attachment_hashes(attachment_id=det.attachment_id, url=url)
    except AttachmentHashError:
        return None
    return ImageHashes(
        phash=computed.phash,
        dhash=computed.dhash,
        whash=computed.whash,
        ahash=computed.ahash,
        mphash=computed.mphash,
        mdhash=computed.mdhash,
        mwhash=computed.mwhash,
        mahash=computed.mahash,
    )


def _hash_ensemble_dict(hashes: ImageHashes) -> dict[str, int]:
    """The 4-hash ensemble as stored on ``Detection.hashes`` (HashSet shape)."""
    return {
        "phash": hashes.phash,
        "dhash": hashes.dhash,
        "whash": hashes.whash,
        "ahash": hashes.ahash,
    }


def _image_hashes_to_guild_hash(hashes: ImageHashes, added_by: int) -> GuildHash:
    return GuildHash(
        hash_id=f"{hashes.phash:016x}",
        phash=hashes.phash,
        dhash=hashes.dhash,
        whash=hashes.whash,
        ahash=hashes.ahash,
        mphash=hashes.mphash,
        mdhash=hashes.mdhash,
        mwhash=hashes.mwhash,
        mahash=hashes.mahash,
        source="review_confirm",
        added_by=added_by,
    )


async def _card_group(
    ctx: InteractionContext, deps: InteractionDeps, det: DetectionFacts
) -> list[DetectionFacts]:
    """Every detection the pressed card covers, lowest id first.

    Images from one message share one card, so its buttons act on all of
    them. The card is identified by the message the button lives on, and only
    trusted when the button's own detection is on it -- a forged custom id
    cannot widen the action to another card. Cards posted before migration
    0012 carry no link and resolve to the single detection, as before.
    """
    assert ctx.guild_id is not None
    if ctx.card_message_id is None:
        return [det]
    on_card = await deps.get_card_detections(ctx.guild_id, ctx.card_message_id)
    if not any(d.detection_id == det.detection_id for d in on_card):
        return [det]
    return on_card


def _distinct_images(dets: Sequence[DetectionFacts]) -> list[DetectionFacts]:
    """One detection per attachment (lowest id), for the per-image writes.

    A confirmed verdict records its own row for the same attachment, so a
    reopened card can list an image twice; hashing, whitelisting and voting
    must still happen once per image.
    """
    seen: set[tuple[int, int]] = set()
    out: list[DetectionFacts] = []
    for det in dets:
        key = (det.message_id, det.attachment_id)
        if key not in seen:
            seen.add(key)
            out.append(det)
    return out


@dataclass(frozen=True, slots=True)
class _Card:
    """One button press, resolved: who pressed what, on which card's detections."""

    ctx: InteractionContext
    deps: InteractionDeps
    guild_id: int
    action: ReviewAction
    detection_id: int
    #: Every detection the card covers (see :func:`_card_group`).
    group: list[DetectionFacts]
    #: One detection per image, for the per-image writes.
    images: list[DetectionFacts]

    async def resolve_images(self) -> list[tuple[DetectionFacts, ImageHashes | None]]:
        return [(det, await _resolve_image_hashes(self.deps, det)) for det in self.images]

    def uploaders(self) -> list[int]:
        return list(dict.fromkeys(d.uploader_id for d in self.group))

    async def mark_all(self, action_taken: str, audit_action: str) -> None:
        for det in self.group:
            await self.deps.set_detection_action(self.guild_id, det.detection_id, action_taken)
            await self.deps.audit(
                self.guild_id, self.ctx.user_id, audit_action, target=str(det.detection_id)
            )

    async def whitelist(
        self, resolved: Sequence[tuple[DetectionFacts, ImageHashes | None]], reason_prefix: str
    ) -> list[GuildWhitelist]:
        """Whitelist each resolved image; the reason is the prefix plus the detection id."""
        created: list[GuildWhitelist] = []
        for det, hashes in resolved:
            if hashes is None:
                continue
            created.append(
                await self.deps.add_whitelist(
                    self.guild_id,
                    GuildWhitelist(
                        phash=hashes.phash,
                        dhash=hashes.dhash,
                        whash=hashes.whash,
                        reason=f"{reason_prefix}{det.detection_id}",
                        added_by=self.ctx.user_id,
                    ),
                )
            )
        return created


async def handle_review_button(
    ctx: InteractionContext, parsed: ParsedCustomId, deps: InteractionDeps
) -> InteractionResponse:
    """Handle a report button after re-checking the clicker's permission.

    Each action requires the permission in :data:`REVIEW_ACTION_PERMISSIONS`;
    the check runs on *this* click's member permissions, never the message's
    original author or any cached value. The detection lookup is guild-scoped,
    so a forged ``custom_id`` carrying another guild's detection id resolves to
    nothing here.

    One card covers every flagged image of a message (see :func:`_card_group`),
    so each action applies to all of them: one delete / ban / unban per
    message or uploader, one hash / whitelist write per image, one state change
    and audit row per detection.
    """
    _require(ctx, review_action_permission(parsed.action))
    assert ctx.guild_id is not None
    primary = await deps.get_detection(ctx.guild_id, parsed.detection_id)
    if primary is None:
        return InteractionResponse(
            "button.detection_missing", {"detection_id": parsed.detection_id}
        )
    group = await _card_group(ctx, deps, primary)
    card = _Card(
        ctx=ctx,
        deps=deps,
        guild_id=ctx.guild_id,
        action=parsed.action,
        detection_id=parsed.detection_id,
        group=group,
        images=_distinct_images(group),
    )
    handler = _HANDLERS.get(parsed.action)
    if handler is None:  # pragma: no cover
        raise InteractionRejected(CommandError.UNKNOWN_FIELD)
    return await handler(card)


async def _confirm_scam(card: _Card) -> InteractionResponse:
    ctx, deps, guild_id = card.ctx, card.deps, card.guild_id
    # All REST/network work runs before the first DB write -- see
    # _resolve_image_hashes on why that ordering is load-bearing. Every
    # image is hashed BEFORE the message is deleted: re-hashing a member
    # report needs the attachment, which the delete takes with it.
    resolved = await card.resolve_images()
    for channel_id, message_id in dict.fromkeys((d.channel_id, d.message_id) for d in card.group):
        await deps.rest_delete_message(channel_id, message_id)
    for det, hashes in resolved:
        if hashes is None:
            continue
        await deps.add_guild_hash(guild_id, _image_hashes_to_guild_hash(hashes, ctx.user_id))
        if det.hashes is None:
            # Member reports are filed without hashes; now that a moderator
            # confirmed and we re-hashed the image, keep the result so the
            # other buttons (whitelist, submit to global) still work after
            # the original message -- just deleted above -- is gone.
            await deps.set_detection_hashes(guild_id, det.detection_id, _hash_ensemble_dict(hashes))
    await card.mark_all("confirmed", "review.confirm_scam")
    hashed = [(det, hashes) for det, hashes in resolved if hashes is not None]
    # A confirmed scam must not stay exempt: lift any whitelist entry
    # that covers it (an earlier misclick), and say so on the card.
    lifted = await _lift_whitelist(
        deps,
        guild_id,
        ctx.user_id,
        [h.phash for _det, h in hashed],
        cause=f"Confirm scam on detection #{card.detection_id}",
    )
    for det, hashes in hashed:
        # Route the confirmation through the same verdict pipeline a live
        # detection uses. Without this, Confirm deleted the single message
        # above and stopped: no ban, and therefore none of the enforcement
        # that follows one -- Discord's cross-channel ban purge, the
        # campaign sweep, the audited action row. A moderator pressing
        # "confirm scam" clearly intends the guild's configured
        # action_policy (e.g. delete + ban) to apply, exactly as it would
        # have if the hash had matched on upload. The card id lets the
        # outcome land on this (folded) card instead of a new one.
        await deps.submit_confirmed_scam(
            guild_id,
            channel_id=det.channel_id,
            message_id=det.message_id,
            attachment_id=det.attachment_id,
            uploader_id=det.uploader_id,
            matched_hash_id=f"{hashes.phash:016x}",
            confirmed_by=ctx.user_id,
            review_card_id=ctx.card_message_id,
            whitelist_removed=len(lifted),
        )
    key = "button.confirmed_scam" if hashed else "button.confirmed_no_hash"
    # Confirm doubles as the global promotion vote -- but only from servers
    # the owner approved AND that opted in. Everyone else's confirm stays
    # purely local; the vote can also be refused (rate limit/reputation)
    # without affecting the local confirm, which already happened above.
    if hashed and await _is_global_participant(deps, guild_id):
        votes = await _cast_global_votes(card, [h for _det, h in hashed])
        if "promoted" in votes:
            key = "button.confirmed_scam_promoted"
        elif votes:
            key = "button.confirmed_scam_voted"
    return InteractionResponse(
        key, {"detection_id": card.detection_id}, **_card_note(card.action, ctx.user_id)
    )


async def _cast_global_votes(card: _Card, hashed: Sequence[ImageHashes]) -> list[str]:
    votes: list[str] = []
    for hashes in hashed:
        vote = await card.deps.global_vote(
            hash_id=f"{hashes.phash:016x}",
            phash=hashes.phash,
            dhash=hashes.dhash,
            whash=hashes.whash,
            voter_user_id=card.ctx.user_id,
            voter_guild_id=card.guild_id,
        )
        if vote is not None:
            votes.append(vote)
            await card.deps.audit(
                card.guild_id, card.ctx.user_id, "global.vote", target=f"{hashes.phash:016x}"
            )
    return votes


async def _false_positive(card: _Card) -> InteractionResponse:
    ctx, deps, guild_id = card.ctx, card.deps, card.guild_id
    resolved = await card.resolve_images()
    # If enforcement already banned the uploader, a false positive must
    # actually free them -- best-effort, before the first DB write. But an
    # unban is a Ban Members power: False positive is on Manage Messages so
    # every moderator can correct a wrong call, and without this check it
    # would be a second, ungated route around the Unban button. A mod
    # without Ban Members still marks the call; the ban is left for someone
    # who holds it, and the card says so to everyone watching.
    can_unban = has_permission(ctx.member_permissions, Permission.BAN_MEMBERS)
    if can_unban:
        for uploader_id in card.uploaders():
            await deps.rest_unban(
                guild_id, uploader_id, reason=reasons.false_positive_reason(card.detection_id)
            )
    created = await card.whitelist(resolved, "false positive: detection #")
    for det in card.group:
        await deps.reverse_detection_action(guild_id, det.detection_id)
        await deps.audit(
            guild_id, ctx.user_id, "review.false_positive", target=str(det.detection_id)
        )
    disputed = [h for _det, h in resolved if h is not None]
    key = "button.marked_false_positive" if disputed else "button.marked_false_positive_no_hash"
    # A false positive from a participating server kills the global entry:
    # revoke immediately and dock the submitter's reputation. One bad
    # community poisoning the shared set costs it credibility; a legitimate
    # mistake self-corrects. Anywhere else the verdict stays local -- the
    # whitelist above still keeps this server from flagging the image.
    if disputed and await _is_global_participant(deps, guild_id):
        for entry in disputed:
            if await deps.global_dispute(f"{entry.phash:016x}"):
                await deps.audit(
                    guild_id, ctx.user_id, "global.dispute", target=f"{entry.phash:016x}"
                )
                key = "button.marked_false_positive_global_revoked"
    note_key = "card.handled" if can_unban else "card.handled_ban_kept"
    return InteractionResponse(
        key,
        {"detection_id": card.detection_id},
        **_card_note(card.action, ctx.user_id, key=note_key, whitelisted=created),
    )


async def _dismiss(card: _Card) -> InteractionResponse:
    # The queue's only no-op exit. Confirm writes the image into the
    # guild blocklist and False positive writes it into the whitelist --
    # both wrong for a report that was simply mistaken, because a
    # whitelisted image is permanently exempt from detection and a
    # member's bad report must never buy that exemption. Dismiss records
    # that a moderator looked and chose to do nothing: no hash write, no
    # whitelist write, no REST call, nothing restored (a member report
    # never deleted anything). It exists so /queue can actually drain --
    # without a terminal state these rows sit at action_taken='none'
    # forever. The reporter is deliberately not told, so mass-reporting
    # cannot be used to probe what does and does not get through.
    await card.mark_all("dismissed", "review.dismiss")
    return InteractionResponse(
        "button.dismissed",
        {"detection_id": card.detection_id},
        **_card_note(card.action, card.ctx.user_id),
    )


async def _ban_uploader(card: _Card) -> InteractionResponse:
    config = await card.deps.get_config(card.guild_id)
    purge_hours = min(int(config.get("ban_purge_hours", 24)), 168)  # Discord caps at 7d
    banned = False
    for uploader_id in card.uploaders():
        banned = (
            await card.deps.rest_ban(
                card.guild_id,
                uploader_id,
                reason=reasons.confirmed_reason(card.detection_id),
                purge_seconds=purge_hours * 3600,
            )
            or banned
        )
    if not banned:
        return InteractionResponse("button.action_failed")
    await card.mark_all("banned", "review.ban_uploader")
    return InteractionResponse(
        "button.uploader_banned", **_card_note(card.action, card.ctx.user_id)
    )


async def _unban(card: _Card) -> InteractionResponse:
    unbanned = False
    for uploader_id in card.uploaders():
        unbanned = (
            await card.deps.rest_unban(
                card.guild_id,
                uploader_id,
                reason=reasons.manual_unban_reason(card.detection_id),
            )
            or unbanned
        )
    if not unbanned:
        return InteractionResponse("button.action_failed")
    await card.deps.audit(
        card.guild_id, card.ctx.user_id, "review.unban", target=str(card.detection_id)
    )
    return InteractionResponse(
        "button.uploader_unbanned", **_card_note(card.action, card.ctx.user_id)
    )


async def _whitelist_image(card: _Card) -> InteractionResponse:
    resolved = await card.resolve_images()
    if all(hashes is None for _det, hashes in resolved):
        return InteractionResponse("button.no_image")
    created: list[GuildWhitelist] = []
    for det, hashes in resolved:
        if hashes is None:
            continue
        created += await card.whitelist([(det, hashes)], "review: detection #")
        await card.deps.audit(
            card.guild_id, card.ctx.user_id, "review.whitelist_image", target=str(det.detection_id)
        )
    return InteractionResponse(
        "button.image_whitelisted",
        **_card_note(card.action, card.ctx.user_id, whitelisted=created),
    )


async def _submit_global(_card: _Card) -> InteractionResponse:
    # Legacy button on cards rendered before global sharing became
    # automatic. Confirm scam now casts the global vote itself (on
    # approved, opted-in servers), so this button only explains itself.
    return InteractionResponse("button.submit_global_removed")


_HANDLERS: dict[ReviewAction, Callable[[_Card], Awaitable[InteractionResponse]]] = {
    ReviewAction.CONFIRM_SCAM: _confirm_scam,
    ReviewAction.FALSE_POSITIVE: _false_positive,
    ReviewAction.DISMISS: _dismiss,
    ReviewAction.BAN_UPLOADER: _ban_uploader,
    ReviewAction.UNBAN: _unban,
    ReviewAction.WHITELIST_IMAGE: _whitelist_image,
    ReviewAction.SUBMIT_GLOBAL: _submit_global,
}
