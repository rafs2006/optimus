"""Moderation orchestration: verdict -> policy -> boundaries -> action -> audit.

The coordinator ties the pure pieces (:mod:`policy`, :mod:`boundaries`) to the
side-effecting ones (:class:`~optimus.services.moderation.actions.ActionExecutor`,
report posting, audit recording) behind injected callables so the whole flow is
testable without a live gateway or database.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from prometheus_client import Counter

from optimus.contracts.events import Action, OcrFindings, Verdict, VerdictEvent
from optimus.core.logging import get_logger
from optimus.i18n import translate
from optimus.services.moderation import reasons
from optimus.services.moderation.actions import (
    ActionExecutor,
    ActionRequest,
    ActionResult,
    Step,
)
from optimus.services.moderation.boundaries import BoundaryRefusal, TargetContext, check_target
from optimus.services.moderation.explain import explain_result
from optimus.services.moderation.failures import FailureKind, classify
from optimus.services.moderation.permissions import PermissionProbe
from optimus.services.moderation.policy import Decision, PolicyInput, PolicyOutcome, decide
from optimus.services.moderation.priority import (
    PriorityDispatcher,
    QueueFullError,
    classify_action,
)
from optimus.services.moderation.review import AUTO_ACTION_PREFIX, ReportData
from optimus.services.moderation.sweep import SweepOutcome

_log = get_logger(__name__)

#: Discord embed field values cap at 1024 chars; leave headroom for the
#: ellipsis marker when a hostile image OCRs into a wall of text.
_OCR_SUMMARY_MAX = 1000


def _ocr_summary(ocr: OcrFindings | None) -> str | None:
    """Render OCR/QR risk findings into one review-card field value."""
    if ocr is None:
        return None
    parts = [f"{ocr.risk_level} (score {ocr.risk_score})"]
    if ocr.signals:
        parts.append("signals: " + ", ".join(ocr.signals))
    if ocr.lookalike_domains:
        parts.append("lookalike: " + ", ".join(ocr.lookalike_domains))
    if ocr.qr_urls:
        # Wrap in backticks so Discord never auto-links a scam URL on the card.
        parts.append("QR: " + ", ".join(f"`{url}`" for url in ocr.qr_urls))
    summary = " | ".join(parts)
    if len(summary) > _OCR_SUMMARY_MAX:
        summary = summary[:_OCR_SUMMARY_MAX] + "…"
    return summary


ACTIONS_TAKEN = Counter(
    "optimus_moderation_actions_total",
    "Moderation actions attempted.",
    ["action", "success"],
)
DECISIONS = Counter(
    "optimus_moderation_decisions_total",
    "Policy decisions made.",
    ["decision"],
)
BOUNDARY_REFUSALS = Counter(
    "optimus_moderation_boundary_refusals_total",
    "Punitive actions downgraded by a privilege boundary.",
    ["reason"],
)


@dataclass(frozen=True, slots=True)
class GuildModConfig:
    """The moderation-relevant configuration for one guild."""

    guild_id: int
    configured_action: Action
    mod_queue_threshold: float
    auto_act_threshold: float
    safe_mode: bool
    locale: str = "en"
    guild_name: str = ""
    review_channel_id: int | None = None
    timeout_seconds: int = 3600
    #: Seconds of the banned user's message history Discord purges across all
    #: channels when a ban executes (native ban-dialog behavior). 0 disables.
    ban_purge_seconds: int = 0
    #: Distinct channels of near matches from one uploader that trigger the
    #: configured action (0 = off), and how long they are counted for.
    spread_channels: int = 0
    spread_window_seconds: int = 600


#: Resolves a guild's moderation config (Redis-cached / DB-backed at runtime).
ConfigResolver = Callable[[int], Awaitable[GuildModConfig]]
#: Resolves a target's privilege context, or ``None`` if the member is gone.
TargetResolver = Callable[[int, int], Awaitable[TargetContext | None]]
#: Posts a report to the review channel and returns the posted message id.
ReportPoster = Callable[[int, ReportData], Awaitable[int | None]]
#: Persists the action taken + an audit row; returns the detection row id (if any).
AuditRecorder = Callable[[VerdictEvent, str, ActionResult], Awaitable[int | None]]
#: Re-renders an open review card in place: (review channel, card id, every
#: report on it). Used when another image of the same message is flagged.
ReportUpdater = Callable[[int, int, Sequence[ReportData]], Awaitable[None]]
#: Stamps ``detections.reported_at`` once a review card has been posted, so
#: the ``/setup`` backlog replay does not re-surface the same row twice, and
#: links the row to its card: (guild, detection, card message id or None).
ReportedStamper = Callable[[int, int, int | None], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CardCleanup:
    """What closing a confirmed uploader's other open cards accomplished."""

    #: Open reports (detections) marked confirmed.
    closed: int = 0
    #: Their review cards deleted from the review channel.
    cards_deleted: int = 0
    #: The scanned messages those reports were about.
    message_ids: tuple[int, ...] = ()


#: Closes every other open card of a confirmed uploader:
#: (guild, uploader, message already handled, moderator, review channel).
CardCloser = Callable[[int, int, int, int, int | None], Awaitable[CardCleanup]]
#: Records a stored action on detection rows: (guild, detection ids, action).
#: Used when a post's earlier, queued images are settled with the post.
DetectionSettler = Callable[[int, Sequence[int], str], Awaitable[None]]

#: Audit actor recorded when the bot itself settles an uploader's cards.
_SYSTEM_ACTOR = 0

#: The punitive steps, for spelling out on the card what was skipped and why.
_PUNITIVE_ACTIONS = (Action.DELETE_TIMEOUT, Action.DELETE_KICK, Action.DELETE_BAN)


class Boundary(StrEnum):
    """What the privilege check did to a punitive action, shown on the card."""

    #: The uploader had already left; banned by user id anyway.
    DEPARTED_BANNED = "departed_banned"
    #: The uploader had already left; timeout/kick cannot apply to a non-member.
    DEPARTED = "departed"
    #: The uploader's roles could not be read, so nothing punitive ran.
    UNVERIFIED = "unverified"


#: A second confirmed verdict for the same uploader within this many seconds
#: (the other images of the pressed card) does not run the sweep again.
SWEEP_DEDUPE_SECONDS = 120

#: How long a message's card stays open for further images of that message.
#: Images of one post are scanned within seconds of each other; the window
#: only bounds memory, it is not a moderation timeout.
OPEN_CARD_TTL_SECONDS = 15 * 60
#: Upper bound on remembered open cards (oldest evicted first).
OPEN_CARD_LIMIT = 512

#: For this long after an uploader is banned or confirmed here, any other
#: hash match of theirs -- a near match or a global-list match, which on its
#: own only asks a moderator -- is deleted quietly and counted on the settled
#: card. The ban itself still rests on this server's own list.
SETTLED_WINDOW_SECONDS = 10 * 60


@dataclass(slots=True)
class _Campaign:
    """A confirmed uploader whose later blocklisted posts are removed quietly."""

    channel_id: int
    card_id: int
    items: list[ReportData]
    expires_at: float
    removed: int = 0
    #: Posts counted in ``removed``: one post with four images counts once.
    removed_messages: set[int] = field(default_factory=set)


@dataclass(slots=True)
class _KeyedLock:
    """A per-message lock plus how many callers hold or await it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@dataclass(slots=True)
class _OpenCard:
    """A card already posted for a message, which later images join."""

    channel_id: int
    card_id: int
    items: list[ReportData]
    opened_at: float = field(default_factory=time.monotonic)


#: Purges the rest of a confirmed scammer's campaign across every channel and
#: harvests the variant hashes. Returns a summary for the review card.
Sweeper = Callable[[VerdictEvent], Awaitable[SweepOutcome]]


class ModerationCoordinator:
    """Decides and applies moderation for each verdict."""

    def __init__(
        self,
        *,
        config: ConfigResolver,
        target: TargetResolver,
        executor: ActionExecutor,
        report: ReportPoster,
        audit: AuditRecorder,
        dispatcher: PriorityDispatcher[ActionResult] | None = None,
        sweep: Sweeper | None = None,
        mark_reported: ReportedStamper | None = None,
        update_report: ReportUpdater | None = None,
        close_cards: CardCloser | None = None,
        settle_detections: DetectionSettler | None = None,
        campaign_window_seconds: int = 24 * 3600,
        requeue_attempts: int = 0,
        requeue_delay_seconds: float = 0.0,
    ) -> None:
        self._config = config
        #: Extra tries for an enforcement that ended ``rate_limited`` (0 = off,
        #: the default for direct construction in tests).
        self._requeue_attempts = max(0, requeue_attempts)
        self._requeue_delay = max(0.0, requeue_delay_seconds)
        self._target = target
        self._executor = executor
        self._report = report
        self._audit = audit
        self._sweep = sweep
        # Optional so unit tests that fake the coordinator with only the
        # collaborators they need keep working; production wires it up in
        # :func:`build_coordinator`.
        self._mark_reported = mark_reported
        # One card per message: without an updater every image posts its own
        # card, which is also the safe fallback when an update fails.
        self._update_report = update_report
        #: Cards that later images of the same message join, keyed by
        #: (guild, message, decided).
        self._open_cards: OrderedDict[tuple[int, int, bool], _OpenCard] = OrderedDict()
        self._card_locks: dict[tuple[int, int], _KeyedLock] = {}
        #: One verdict at a time per uploader, so a burst across channels
        #: settles once: the first image bans and posts the card, the rest
        #: find the uploader settled and only add to that card.
        self._uploader_locks: dict[tuple[int, int], _KeyedLock] = {}
        # After a moderator confirms an uploader, their other open cards are
        # closed and their later blocklisted posts are deleted without a card
        # of their own (counted on the confirmed card instead).
        self._close_cards = close_cards
        self._settle_detections = settle_detections
        self._campaign_window = campaign_window_seconds
        self._campaigns: OrderedDict[tuple[int, int], _Campaign] = OrderedDict()
        self._recent_sweeps: dict[tuple[int, int], float] = {}
        #: When each uploader was last settled (auto-handled or confirmed),
        #: recorded as soon as enforcement ran -- before the card is posted,
        #: so a post checked at the same moment can see it.
        self._settled: OrderedDict[tuple[int, int], float] = OrderedDict()
        #: Per uploader, the channels their near matches were seen in and
        #: when, for the spread-across-channels rule.
        self._spread: OrderedDict[tuple[int, int], dict[int, float]] = OrderedDict()
        # When set, enforcement runs through the priority dispatcher so PROTECT
        # actions are dispatched ahead of courtesy work under rate-limit
        # pressure. None preserves the direct, synchronous execution path.
        self._dispatcher = dispatcher

    def attach_permission_probe(self, probe: PermissionProbe) -> None:
        """Give the executor a permission probe once the gateway cache exists."""
        self._executor.attach_probe(probe)

    async def handle_verdict(self, event: VerdictEvent) -> ActionResult:
        """Process one verdict end-to-end and return the action outcome.

        Serialised per uploader. Two posts of one scam account seconds apart
        used to run side by side: each banned, each posted a card, and each
        settlement then removed the other's card. One at a time, the first
        image settles the uploader and every later one joins its card.
        """
        async with self._keyed_lock(self._uploader_locks, (event.guild_id, event.uploader_id)):
            return await self._handle_verdict(event)

    async def _handle_verdict(self, event: VerdictEvent) -> ActionResult:
        cfg = await self._config(event.guild_id)
        followup = await self._remove_followup(event, cfg)
        if followup is not None:
            return followup
        outcome = decide(
            PolicyInput(
                verdict=event.verdict,
                confidence=event.confidence,
                configured_action=cfg.configured_action,
                mod_queue_threshold=cfg.mod_queue_threshold,
                auto_act_threshold=cfg.auto_act_threshold,
                safe_mode=cfg.safe_mode,
                global_match=event.matched_source == "global",
            )
        )
        spread = self._spread_channels(event, cfg, outcome)
        if spread:
            outcome = PolicyOutcome(Decision.AUTO_ACT, cfg.configured_action, "spread_channels")
        DECISIONS.labels(decision=outcome.decision.value).inc()
        # One line per image, so "why did this need a moderator?" is answered
        # by the log rather than guessed.
        _log.info(
            "verdict_decided",
            guild_id=event.guild_id,
            message_id=event.message_id,
            uploader_id=event.uploader_id,
            verdict=event.verdict.value,
            confidence=event.confidence,
            matched_source=event.matched_source,
            decision=outcome.decision.value,
            reason=outcome.reason,
            confirmed=event.confirmed_by is not None,
            spread_channels=spread or None,
        )

        action = outcome.action
        decision = outcome.decision
        boundary: Boundary | None = None

        if decision is Decision.AUTO_ACT and action in _PUNITIVE_ACTIONS:
            intended = action
            action, decision, boundary = await self._apply_boundaries(event, action, decision)
        else:
            intended = action

        if decision is Decision.NONE:
            return ActionResult(Action.NONE, success=True, detail=outcome.reason)

        result = await self._execute(event, cfg, action, decision)
        auto = _fully_handled(event, cfg, decision, action, result)
        if auto or (event.confirmed_by is not None and result.success):
            self._mark_settled(event)
        # Enforcement landed on a real scam, so clean up the rest of the
        # campaign. Deliberately NOT gated on ``result.success``: the whole
        # point of the sweep is to cover the case where the punitive half
        # failed (no Ban Members permission, role hierarchy, account already
        # gone) and Discord's native ban purge therefore never ran, leaving
        # every other copy standing. That failure mode is precisely what made
        # a delete_ban policy behave like "deleted one message".
        swept = await self._sweep_campaign(event, decision, action)
        cleanup = (
            await self._close_uploader_cards(event, cfg, auto=auto) if swept is not None else None
        )
        # A card the bot settled itself is stored as settled, so a later
        # cleanup never mistakes it for one still waiting on a moderator.
        stored = f"{AUTO_ACTION_PREFIX}{action.value}" if auto else action.value
        detection_id = await self._audit(event, stored, result)
        await self._post_report(
            event,
            cfg,
            action,
            detection_id,
            result,
            swept,
            cleanup,
            boundary=boundary,
            intended=intended,
            auto=auto,
            spread=spread,
        )
        return result

    def _spread_channels(
        self, event: VerdictEvent, cfg: GuildModConfig, outcome: PolicyOutcome
    ) -> int:
        """Channels this uploader spread near matches across, once that triggers action.

        Returns 0 unless the rule fires. Counts only what would otherwise ask a
        moderator about this server's own list: a near match, not a global
        match, a member report or a risk scan. Never fires in safe mode or
        under a report-only policy, and the privilege boundaries still apply
        to the action, so a moderator posting in many channels is not banned.
        """
        if cfg.spread_channels <= 0 or outcome.reason != "queued_for_review":
            return 0
        if event.verdict is not Verdict.SCAM or event.confirmed_by is not None:
            return 0
        if event.reported_by is not None or event.matched_source != "guild":
            return 0
        if not event.matched_hash_id or cfg.safe_mode:
            return 0
        if cfg.configured_action in (Action.NONE, Action.REPORT_ONLY):
            return 0
        if self._recently_settled(event):
            # Already banned or confirmed: their later posts are only deleted
            # (see :meth:`_remove_followup`). One that reached here had its
            # delete refused, and that must stay visible on an open card.
            return 0
        key = (event.guild_id, event.uploader_id)
        now = time.monotonic()
        seen = {
            c: t
            for c, t in self._spread.get(key, {}).items()
            if now - t <= cfg.spread_window_seconds
        }
        seen[event.channel_id] = now
        self._spread[key] = seen
        self._spread.move_to_end(key)
        while len(self._spread) > OPEN_CARD_LIMIT:
            self._spread.popitem(last=False)
        if len(seen) < cfg.spread_channels:
            return 0
        _log.info(
            "spread_escalated",
            guild_id=event.guild_id,
            uploader_id=event.uploader_id,
            message_id=event.message_id,
            channels=len(seen),
            window_seconds=cfg.spread_window_seconds,
        )
        return len(seen)

    async def _sweep_campaign(
        self, event: VerdictEvent, decision: Decision, action: Action
    ) -> SweepOutcome | None:
        """Purge the uploader's other posts, when this verdict warranted action.

        A moderator's confirmation (Confirm scam, "Review as scam") always
        sweeps, whatever the server's ``action_policy``: the policy governs what
        the bot does on its own, and here a moderator already made the call.
        The other images of the same card arrive as their own confirmed
        verdicts moments later; they do not sweep the same uploader again.
        """
        if self._sweep is None:
            return None
        if event.confirmed_by is not None:
            key = (event.guild_id, event.uploader_id)
            now = time.monotonic()
            last = self._recent_sweeps.get(key)
            if last is not None and now - last < SWEEP_DEDUPE_SECONDS:
                return None
            self._recent_sweeps = {
                k: t for k, t in self._recent_sweeps.items() if now - t < SWEEP_DEDUPE_SECONDS
            }
            self._recent_sweeps[key] = now
        elif decision is not Decision.AUTO_ACT or action in (Action.NONE, Action.REPORT_ONLY):
            return None
        try:
            return await self._sweep(event)
        except Exception:
            # Best-effort cleanup: the primary action already ran and was
            # audited, and a bus redelivery would only re-run it into a
            # "duplicate". Never fail the verdict over the sweep.
            _log.error(
                "campaign_sweep_failed",
                guild_id=event.guild_id,
                uploader_id=event.uploader_id,
                exc_info=True,
            )
            return None

    async def _apply_boundaries(
        self, event: VerdictEvent, action: Action, decision: Decision
    ) -> tuple[Action, Decision, Boundary | None]:
        ctx = await self._target(event.guild_id, event.uploader_id)
        if ctx is None:
            # The uploader's privileges could not be read (a 403, a transient
            # 5xx). Never punish blind -- they may be an admin -- but the scam
            # message itself must still come down.
            BOUNDARY_REFUSALS.labels(reason="unverified").inc()
            return Action.DELETE, decision, Boundary.UNVERIFIED
        result = check_target(ctx)
        if not result.allowed:
            reason = result.refusal.value if result.refusal else "unknown"
            BOUNDARY_REFUSALS.labels(reason=reason).inc()
            if result.refusal is BoundaryRefusal.NOT_IN_GUILD:
                # The uploader already left (scam accounts post and leave).
                # Discord bans by user id, members or not, and a non-member
                # holds no roles, so the ban still runs: without it they
                # simply rejoin. Timeout and kick need a member, so those
                # policies fall back to deleting the post.
                if action is Action.DELETE_BAN:
                    return action, decision, Boundary.DEPARTED_BANNED
                return Action.DELETE, decision, Boundary.DEPARTED
            return Action.REPORT_ONLY, Decision.MOD_QUEUE, None
        return action, decision, None

    async def _execute(
        self, event: VerdictEvent, cfg: GuildModConfig, action: Action, decision: Decision
    ) -> ActionResult:
        if decision is Decision.MOD_QUEUE or action in (Action.NONE, Action.REPORT_ONLY):
            ACTIONS_TAKEN.labels(action=Action.REPORT_ONLY.value, success="true").inc()
            return ActionResult(Action.REPORT_ONLY, success=True, detail="queued")
        request = ActionRequest(
            guild_id=event.guild_id,
            channel_id=event.channel_id,
            message_id=event.message_id,
            uploader_id=event.uploader_id,
            action=action,
            idempotency_key=f"modact:{event.idempotency_key}:{action.value}",
            guild_name=cfg.guild_name,
            locale=cfg.locale,
            timeout_seconds=cfg.timeout_seconds,
            ban_purge_seconds=cfg.ban_purge_seconds,
            # Without this the audit log recorded only ActionRequest's bare
            # default for every automated removal: no confidence, no
            # fingerprint, no message. The audit log is the only record a
            # moderator can consult afterwards, so it carries the evidence.
            reason=reasons.auto_reason(
                confidence=event.confidence,
                matched_hash_id=event.matched_hash_id,
                matched_source=event.matched_source,
                message_id=event.message_id,
            ),
        )
        result = await self._dispatch(action, request)
        for attempt in range(1, self._requeue_attempts + 1):
            if not _rate_limited(result):
                break
            # Being rate limited is "not yet", never "no": try again shortly
            # rather than leave a confirmed scam for a moderator to finish.
            # A fresh key, since a step-level 429 already claimed the first.
            _log.info(
                "moderation_action_requeued",
                guild_id=event.guild_id,
                message_id=event.message_id,
                action=action.value,
                attempt=attempt,
            )
            await asyncio.sleep(self._requeue_delay)
            retry = replace(request, idempotency_key=f"{request.idempotency_key}:r{attempt}")
            result = await self._dispatch(action, retry)
        ACTIONS_TAKEN.labels(action=action.value, success=str(result.success).lower()).inc()
        return result

    async def _dispatch(self, action: Action, request: ActionRequest) -> ActionResult:
        """Run enforcement, through the priority dispatcher when one is wired.

        Without a dispatcher this is the original direct call. With one, the
        executor call is submitted at the action's priority and awaited; a
        full-queue rejection (only possible for droppable classes — PROTECT is
        always admitted) surfaces as a ``dropped`` failure so the caller still
        records an audit row.
        """
        if self._dispatcher is None:
            return await self._executor.execute(request)
        try:
            future = await self._dispatcher.submit(
                classify_action(action), lambda: self._executor.execute(request)
            )
        except QueueFullError:
            return ActionResult(action, success=False, detail="dropped")
        return await future

    async def _post_report(
        self,
        event: VerdictEvent,
        cfg: GuildModConfig,
        action: Action,
        detection_id: int | None,
        result: ActionResult,
        swept: SweepOutcome | None = None,
        cleanup: CardCleanup | None = None,
        *,
        boundary: Boundary | None = None,
        intended: Action | None = None,
        auto: bool = False,
        spread: int = 0,
    ) -> None:
        if cfg.review_channel_id is None or detection_id is None:
            return
        # The report doubles as the guild's status feed: surface the actual
        # outcome, not just the intended action, so a failed enforcement is
        # visible in Discord instead of only in an audit row.
        action_taken = (
            action.value if result.success else f"{action.value} (failed: {result.detail})"
        )
        if boundary is Boundary.DEPARTED_BANNED and not _banned(result):
            # "banned by user ID" next to a failed ban told moderators the
            # opposite of what happened; the failure text says it alone.
            boundary = None
        if boundary is not None:
            # Say why the configured punishment did or did not happen, instead
            # of a bare "delete" that reads like the policy was ignored.
            note = translate(
                f"report.boundary_{boundary.value}", cfg.locale, action=(intended or action).value
            )
            action_taken = f"{action_taken} — {note}"
        if spread:
            # Say why a near match was acted on: the confidence on the card
            # is unchanged, so without this it reads like the bar was ignored.
            note = translate(
                "report.spread_channels",
                cfg.locale,
                channels=spread,
                minutes=max(1, cfg.spread_window_seconds // 60),
            )
            action_taken = f"{action_taken} — {note}"
        # Whatever could not be applied is spelled out as an instruction on the
        # card. Without this, a channel the bot cannot see produced a report
        # that looked like a silent, inexplicable failure.
        problem = explain_result(result, cfg.locale, channel_id=event.channel_id)
        if swept is not None and swept.touched:
            # Make the cross-channel cleanup visible to moderators: without it
            # the card reports one deletion while the sweep quietly removed a
            # campaign spanning a dozen channels.
            extra = f"purged {swept.deleted} more in {swept.channels} channels"
            if swept.failed:
                extra += f", {swept.failed} unreachable"
            if swept.harvested:
                extra += f", +{len(swept.harvested)} hashes blocklisted"
            action_taken = f"{action_taken} — {extra}"
        if cleanup is not None and cleanup.closed:
            action_taken = (
                f"{action_taken} — cleared {cleanup.closed} other report(s) from this uploader"
            )
        data = ReportData(
            detection_id=detection_id,
            guild_id=event.guild_id,
            channel_id=event.channel_id,
            message_id=event.message_id,
            uploader_id=event.uploader_id,
            verdict=event.verdict.value,
            confidence=event.confidence,
            action_taken=action_taken,
            matched_hash_id=event.matched_hash_id,
            global_match=event.matched_source == "global",
            reported_by=event.reported_by,
            # Show the image only while it still exists. A member report
            # deletes nothing, and a delete that was refused for want of
            # permission leaves it up too -- both are precisely the cards
            # a moderator has to eyeball before pressing Confirm.
            image_url=None if result.message_deleted else event.source_url,
            ocr_summary=_ocr_summary(event.ocr),
            problem=problem,
            partial=result.partial,
            locale=cfg.locale,
            whitelist_removed=event.whitelist_removed,
        )
        key = (event.guild_id, event.message_id)
        # A moderator's confirmation is already decided: its card is folded
        # (outcome, no buttons) and kept apart from the message's open card.
        # So is a card the bot settled on its own (see :func:`_fully_handled`):
        # it is posted folded too, keeping only the False positive button.
        decided = event.confirmed_by is not None or auto
        card_key = (event.guild_id, event.message_id, decided)
        if event.confirmed_by is not None:
            data = replace(data, decided_by=event.confirmed_by)
        elif auto:
            data = replace(data, auto_handled=True)
        async with self._keyed_lock(self._card_locks, key):
            if decided:
                # The open card is settled now; later images must not reopen it.
                stale = self._open_cards.pop((event.guild_id, event.message_id, False), None)
                if auto and stale is not None and card_key not in self._open_cards:
                    # Near matches of this post went first and opened a card;
                    # this image handled the post on its own. The post is gone
                    # with all its images, so that card is this one: fold it
                    # in place instead of leaving its buttons on a dead post.
                    await self._adopt_open_card(event, card_key, stale)
                if event.review_card_id is not None and card_key not in self._open_cards:
                    # Confirm was pressed on a card: write the outcome onto it.
                    self._open_cards[card_key] = _OpenCard(
                        channel_id=cfg.review_channel_id, card_id=event.review_card_id, items=[]
                    )
            if await self._join_open_card(card_key, cfg.review_channel_id, data, detection_id):
                if decided:
                    self._start_campaign(
                        event, cfg.review_channel_id, self._open_cards[card_key].card_id
                    )
                return
            try:
                card_id = await self._report(cfg.review_channel_id, data)
            except Exception as exc:
                # Posting the report is best-effort status: a failure here (missing
                # send permission in the review channel, deleted channel) must not
                # fail the verdict handler — the action already ran and was audited,
                # and a bus redelivery would only re-run it into a "duplicate".
                # The stamper is intentionally NOT called on this path either:
                # ``reported_at IS NULL`` is what makes the row eligible for the
                # ``/setup`` backlog replay, so a failed post stays eligible.
                #
                # The cause is classified because this log line is the *only* signal
                # left when the review channel itself is unreachable: "missing
                # access to the review channel" is actionable, a bare traceback is
                # not.
                failure = classify(exc)
                _log.error(
                    "review_report_failed",
                    guild_id=event.guild_id,
                    channel_id=cfg.review_channel_id,
                    detection_id=detection_id,
                    cause=failure.detail,
                    permission_related=failure.permission_related,
                    exc_info=True,
                )
                return
            # Stamp only after a successful post: an exception above returned
            # already, and the retention purge does not care about this column
            # (its cutoff is ``created_at``). A stamp failure here is best-effort
            # -- the card is already in Discord, and losing the stamp would only
            # surface as a duplicate card on the next ``/setup``, not as a
            # correctness problem.
            if card_id is not None:
                self._remember_card(card_key, cfg.review_channel_id, card_id, data)
                if decided:
                    self._start_campaign(event, cfg.review_channel_id, card_id)
            if self._mark_reported is not None:
                with contextlib.suppress(Exception):
                    await self._mark_reported(event.guild_id, detection_id, card_id)
        if not decided and card_id is not None:
            await self._settle_late_card(event, cfg)

    async def _adopt_open_card(
        self, event: VerdictEvent, card_key: tuple[int, int, bool], card: _OpenCard
    ) -> None:
        """Take over a post's open card when a later image of it was auto-handled.

        The earlier images are recorded as deleted with the post (``auto:delete``,
        like a joined image), so no cleanup, ``/queue`` or replay treats them as
        still waiting on a moderator. The card then joins the auto-handled
        image and renders folded.
        """
        card.items = [
            replace(
                i,
                action_taken=Action.DELETE.value,
                auto_handled=True,
                image_url=None,
                extra_image_urls=(),
                evidence_url=None,
                problem=None,
            )
            for i in card.items
        ]
        self._open_cards[card_key] = card
        if self._settle_detections is None:
            return
        ids = [i.detection_id for i in card.items]
        try:
            await self._settle_detections(
                event.guild_id, ids, f"{AUTO_ACTION_PREFIX}{Action.DELETE.value}"
            )
        except Exception:
            # The card still folds; the rows stay open for /queue, which is safe.
            _log.warning(
                "same_post_settle_failed",
                guild_id=event.guild_id,
                message_id=event.message_id,
                detections=ids,
                exc_info=True,
            )

    async def _settle_late_card(self, event: VerdictEvent, cfg: GuildModConfig) -> None:
        """Close an open card the uploader's own settlement raced past.

        Two posts checked at the same moment: one is handled and settles the
        uploader, its cleanup runs, and only then does the other's open card
        land -- too late for that cleanup. Checked after the card is stamped,
        so either the settling post's cleanup saw it or this check sees the
        settlement. The post is deleted (already gone counts) and the card
        closed and removed exactly as that cleanup would have.
        """
        if not self._late_card_eligible(event, cfg):
            return
        result = await self._execute(event, cfg, Action.DELETE, Decision.AUTO_ACT)
        if not result.success:
            return  # the open card stays: a refused delete must stay visible
        await self._close_cards_for(event, cfg)
        campaign = self._campaigns.get((event.guild_id, event.uploader_id))
        if campaign is None:
            return
        campaign.removed += 1
        if self._update_report is not None and campaign.items:
            items = [replace(i, followups_removed=campaign.removed) for i in campaign.items]
            with contextlib.suppress(Exception):
                await self._update_report(campaign.channel_id, campaign.card_id, items)

    def _late_card_eligible(self, event: VerdictEvent, cfg: GuildModConfig) -> bool:
        if event.confirmed_by is not None or event.reported_by is not None:
            return False
        if cfg.safe_mode or not event.matched_hash_id:
            return False
        return self._recently_settled(event)

    async def _close_cards_for(self, event: VerdictEvent, cfg: GuildModConfig) -> None:
        if self._close_cards is None:
            return
        try:
            # keep_message_id 0 matches no message: every open card of this
            # uploader goes, including the one just posted.
            cleanup = await self._close_cards(
                event.guild_id, event.uploader_id, 0, _SYSTEM_ACTOR, cfg.review_channel_id
            )
        except Exception:
            _log.error(
                "campaign_card_close_failed",
                guild_id=event.guild_id,
                uploader_id=event.uploader_id,
                exc_info=True,
            )
            return
        for message_id in cleanup.message_ids:
            self._open_cards.pop((event.guild_id, message_id, False), None)

    def _mark_settled(self, event: VerdictEvent) -> None:
        key = (event.guild_id, event.uploader_id)
        self._settled[key] = time.monotonic()
        self._settled.move_to_end(key)
        while len(self._settled) > OPEN_CARD_LIMIT:
            self._settled.popitem(last=False)

    def _recently_settled(self, event: VerdictEvent) -> bool:
        at = self._settled.get((event.guild_id, event.uploader_id))
        return at is not None and time.monotonic() - at <= SETTLED_WINDOW_SECONDS

    @contextlib.asynccontextmanager
    async def _keyed_lock(
        self, locks: dict[tuple[int, int], _KeyedLock], key: tuple[int, int]
    ) -> AsyncIterator[None]:
        """Serialise work per key: card posting per message, verdicts per uploader.

        Without the card lock, two images of one post finishing together
        would both see "no card yet" and post two. Keyed rather than global
        so one busy campaign does not queue every other server behind it. The
        lock entry is dropped once nobody holds or waits on it.
        """
        entry = locks.setdefault(key, _KeyedLock())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            # Counted rather than ``lock.locked()``: right after a release the
            # lock reads unlocked while a waiter is still about to take it, and
            # dropping the entry then would hand a newcomer a second lock.
            entry.users -= 1
            if entry.users == 0:
                del locks[key]

    def _remember_card(
        self, key: tuple[int, int, bool], channel_id: int, card_id: int, data: ReportData
    ) -> None:
        self._open_cards[key] = _OpenCard(channel_id=channel_id, card_id=card_id, items=[data])
        self._open_cards.move_to_end(key)
        while len(self._open_cards) > OPEN_CARD_LIMIT:
            self._open_cards.popitem(last=False)

    async def _join_open_card(
        self, key: tuple[int, int, bool], channel_id: int, data: ReportData, detection_id: int
    ) -> bool:
        """Add ``data`` to the message's open card; ``False`` when a new card is needed.

        Falls back to a new card -- never to silence -- when there is no open
        card, it expired or moved channel, no updater is wired, or Discord
        refused the edit (e.g. a moderator deleted the card).
        """
        card = self._open_cards.get(key)
        if card is None or self._update_report is None:
            return False
        if card.channel_id != channel_id or (
            time.monotonic() - card.opened_at > OPEN_CARD_TTL_SECONDS
        ):
            del self._open_cards[key]
            return False
        # A bus redelivery of an image already on the card replaces its entry.
        items = [i for i in card.items if i.detection_id != data.detection_id] + [data]
        items.sort(key=lambda i: i.detection_id)
        try:
            await self._update_report(channel_id, card.card_id, items)
        except Exception:
            _log.warning(
                "review_card_update_failed",
                guild_id=key[0],
                card_id=card.card_id,
                detection_id=detection_id,
                exc_info=True,
            )
            del self._open_cards[key]
            return False
        card.items = items
        if self._mark_reported is not None:
            with contextlib.suppress(Exception):
                await self._mark_reported(key[0], detection_id, card.card_id)
        return True

    async def _close_uploader_cards(
        self, event: VerdictEvent, cfg: GuildModConfig, *, auto: bool = False
    ) -> CardCleanup | None:
        """After a confirmation, close and remove the uploader's other cards.

        One confirmation settles the whole campaign, so the other cards that
        account produced (one per message, across channels) are marked
        confirmed and deleted from the review channel rather than left for a
        moderator to click through one by one. A post the bot fully handled on
        its own (``auto``) settles the campaign the same way, recorded under
        the system actor. Best-effort: a failure leaves the cards open, which
        is safe.
        """
        if self._close_cards is None:
            return None
        actor = event.confirmed_by if event.confirmed_by is not None else None
        if actor is None and auto:
            actor = _SYSTEM_ACTOR
        if actor is None:
            return None
        try:
            cleanup = await self._close_cards(
                event.guild_id,
                event.uploader_id,
                event.message_id,
                actor,
                cfg.review_channel_id,
            )
        except Exception:
            _log.error(
                "campaign_card_close_failed",
                guild_id=event.guild_id,
                uploader_id=event.uploader_id,
                exc_info=True,
            )
            return None
        # Those messages' cards are gone: a late image must not try to join one.
        for message_id in cleanup.message_ids:
            self._open_cards.pop((event.guild_id, message_id, False), None)
        return cleanup

    def _start_campaign(self, event: VerdictEvent, channel_id: int, card_id: int) -> None:
        """Remember a confirmed uploader so their later reposts do not get cards."""
        key = (event.guild_id, event.uploader_id)
        card = self._open_cards.get((event.guild_id, event.message_id, True))
        items = list(card.items) if card is not None else []
        current = self._campaigns.get(key)
        if current is not None and current.card_id == card_id:
            current.items = items or current.items
            return
        self._campaigns[key] = _Campaign(
            channel_id=channel_id,
            card_id=card_id,
            items=items,
            expires_at=time.monotonic() + self._campaign_window,
        )
        self._campaigns.move_to_end(key)
        while len(self._campaigns) > OPEN_CARD_LIMIT:
            self._campaigns.popitem(last=False)

    async def _remove_followup(
        self, event: VerdictEvent, cfg: GuildModConfig
    ) -> ActionResult | None:
        """Quietly delete a confirmed uploader's later blocklisted repost.

        Within the campaign window after a moderator confirmed an uploader, a
        new post of theirs that matches this server's blocklist is deleted
        without a card of its own; the confirmed card counts it instead
        ("+N later posts removed"). Anything else -- another account, a risk
        scan without a hash match, a global-only match, safe mode -- takes the
        normal path, and so does a delete Discord refuses, so a failure is
        always visible on a card.
        """
        key = (event.guild_id, event.uploader_id)
        campaign = self._campaigns.get(key)
        if campaign is None or event.confirmed_by is not None:
            return None
        if time.monotonic() > campaign.expires_at:
            del self._campaigns[key]
            return None
        if cfg.safe_mode or not event.matched_hash_id:
            return None
        if event.matched_source != "guild" and not self._recently_settled(event):
            return None
        # The uploader is settled: the ban (or the moderator's call) already
        # happened, so this image needs only its post gone -- no second ban
        # racing the first into a rate limit, and no card of its own.
        result = await self._execute(event, cfg, Action.DELETE, Decision.AUTO_ACT)
        if not result.success:
            return None  # a refused delete must stay visible on a card
        detection_id = await self._audit(
            event, f"{AUTO_ACTION_PREFIX}{Action.DELETE.value}", result
        )
        same_post = any(i.message_id == event.message_id for i in campaign.items)
        if same_post and detection_id is None:
            # Deleted, but with no record to show; the log keeps it.
            _log.warning(
                "followup_image_unrecorded",
                guild_id=event.guild_id,
                message_id=event.message_id,
                attachment_id=event.attachment_id,
            )
        elif same_post and campaign.items and detection_id is not None:
            # Another image of the post on the card: shown as one more image.
            # Only this image's own outcome -- a plain delete -- never the
            # first image's notes, which the card would otherwise add up
            # once per copy ("purged 12 more" for one sweep of 3).
            campaign.items.append(
                replace(
                    campaign.items[0],
                    detection_id=detection_id,
                    confidence=event.confidence,
                    action_taken=Action.DELETE.value,
                    matched_hash_id=event.matched_hash_id,
                    global_match=event.matched_source == "global",
                    evidence_url=None,
                    image_url=None,
                    problem=None,
                    partial=False,
                    ocr_summary=_ocr_summary(event.ocr),
                    whitelist_removed=event.whitelist_removed,
                )
            )
            card = self._open_cards.get((event.guild_id, event.message_id, True))
            if card is not None and card.card_id == campaign.card_id:
                card.items = list(campaign.items)
        elif event.message_id not in campaign.removed_messages:
            campaign.removed_messages.add(event.message_id)
            campaign.removed = len(campaign.removed_messages)
        if self._update_report is not None and campaign.items:
            items = [replace(i, followups_removed=campaign.removed) for i in campaign.items]
            with contextlib.suppress(Exception):
                await self._update_report(campaign.channel_id, campaign.card_id, items)
        if self._mark_reported is not None and detection_id is not None:
            # Stamped onto the confirmed card, so the /setup replay never
            # resurfaces it as a card of its own.
            with contextlib.suppress(Exception):
                await self._mark_reported(event.guild_id, detection_id, campaign.card_id)
        return result


def _fully_handled(
    event: VerdictEvent,
    cfg: GuildModConfig,
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
    if action is not cfg.configured_action:
        return False
    return result.success and bool(result.steps) and result.message_deleted


def _banned(result: ActionResult) -> bool:
    """Whether the ban step of ``result`` actually succeeded."""
    return any(s.step is Step.BAN and s.success for s in result.steps)


def _rate_limited(result: ActionResult) -> bool:
    """Whether ``result`` failed only for want of rate budget (ours or Discord's)."""
    if result.success:
        return False
    if result.detail == "rate_limited":
        return True
    return any(
        s.failure is not None and s.failure.kind is FailureKind.RATE_LIMITED
        for s in result.failed_steps
    )
