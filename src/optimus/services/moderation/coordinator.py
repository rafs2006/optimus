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

from prometheus_client import Counter

from optimus.contracts.events import Action, OcrFindings, VerdictEvent
from optimus.core.logging import get_logger
from optimus.services.moderation import reasons
from optimus.services.moderation.actions import ActionExecutor, ActionRequest, ActionResult
from optimus.services.moderation.boundaries import BoundaryRefusal, TargetContext, check_target
from optimus.services.moderation.explain import explain_result
from optimus.services.moderation.failures import classify
from optimus.services.moderation.permissions import PermissionProbe
from optimus.services.moderation.policy import Decision, PolicyInput, decide
from optimus.services.moderation.priority import (
    PriorityDispatcher,
    QueueFullError,
    classify_action,
)
from optimus.services.moderation.review import ReportData
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

#: A second confirmed verdict for the same uploader within this many seconds
#: (the other images of the pressed card) does not run the sweep again.
SWEEP_DEDUPE_SECONDS = 120

#: How long a message's card stays open for further images of that message.
#: Images of one post are scanned within seconds of each other; the window
#: only bounds memory, it is not a moderation timeout.
OPEN_CARD_TTL_SECONDS = 15 * 60
#: Upper bound on remembered open cards (oldest evicted first).
OPEN_CARD_LIMIT = 512


@dataclass(slots=True)
class _Campaign:
    """A confirmed uploader whose later blocklisted posts are removed quietly."""

    channel_id: int
    card_id: int
    items: list[ReportData]
    expires_at: float
    removed: int = 0


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
        campaign_window_seconds: int = 24 * 3600,
    ) -> None:
        self._config = config
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
        # After a moderator confirms an uploader, their other open cards are
        # closed and their later blocklisted posts are deleted without a card
        # of their own (counted on the confirmed card instead).
        self._close_cards = close_cards
        self._campaign_window = campaign_window_seconds
        self._campaigns: OrderedDict[tuple[int, int], _Campaign] = OrderedDict()
        self._recent_sweeps: dict[tuple[int, int], float] = {}
        # When set, enforcement runs through the priority dispatcher so PROTECT
        # actions are dispatched ahead of courtesy work under rate-limit
        # pressure. None preserves the direct, synchronous execution path.
        self._dispatcher = dispatcher

    def attach_permission_probe(self, probe: PermissionProbe) -> None:
        """Give the executor a permission probe once the gateway cache exists."""
        self._executor.attach_probe(probe)

    async def handle_verdict(self, event: VerdictEvent) -> ActionResult:
        """Process one verdict end-to-end and return the action outcome."""
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
        DECISIONS.labels(decision=outcome.decision.value).inc()

        action = outcome.action
        decision = outcome.decision

        if decision is Decision.AUTO_ACT and action in (
            Action.DELETE_TIMEOUT,
            Action.DELETE_KICK,
            Action.DELETE_BAN,
        ):
            action, decision = await self._apply_boundaries(event, action, decision)

        if decision is Decision.NONE:
            return ActionResult(Action.NONE, success=True, detail=outcome.reason)

        result = await self._execute(event, cfg, action, decision)
        # Enforcement landed on a real scam, so clean up the rest of the
        # campaign. Deliberately NOT gated on ``result.success``: the whole
        # point of the sweep is to cover the case where the punitive half
        # failed (no Ban Members permission, role hierarchy, account already
        # gone) and Discord's native ban purge therefore never ran, leaving
        # every other copy standing. That failure mode is precisely what made
        # a delete_ban policy behave like "deleted one message".
        swept = await self._sweep_campaign(event, decision, action)
        cleanup = await self._close_uploader_cards(event, cfg) if swept is not None else None
        detection_id = await self._audit(event, action.value, result)
        await self._post_report(event, cfg, action, detection_id, result, swept, cleanup)
        return result

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
    ) -> tuple[Action, Decision]:
        ctx = await self._target(event.guild_id, event.uploader_id)
        if ctx is None:
            # The uploader is gone (left, or already banned). The punitive half
            # is impossible, but the scam message itself must still be removed —
            # downgrading all the way to report-only would leave old scam posts
            # standing whenever the scammer has already departed.
            BOUNDARY_REFUSALS.labels(reason="not_in_guild").inc()
            return Action.DELETE, decision
        result = check_target(ctx)
        if not result.allowed:
            reason = result.refusal.value if result.refusal else "unknown"
            BOUNDARY_REFUSALS.labels(reason=reason).inc()
            if result.refusal is BoundaryRefusal.NOT_IN_GUILD:
                return Action.DELETE, decision
            return Action.REPORT_ONLY, Decision.MOD_QUEUE
        return action, decision

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
    ) -> None:
        if cfg.review_channel_id is None or detection_id is None:
            return
        # The report doubles as the guild's status feed: surface the actual
        # outcome, not just the intended action, so a failed enforcement is
        # visible in Discord instead of only in an audit row.
        action_taken = (
            action.value if result.success else f"{action.value} (failed: {result.detail})"
        )
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
        decided = event.confirmed_by is not None
        card_key = (event.guild_id, event.message_id, decided)
        if decided:
            data = replace(data, decided_by=event.confirmed_by)
        async with self._card_lock(key):
            if decided:
                # The open card is settled now; later images must not reopen it.
                self._open_cards.pop((event.guild_id, event.message_id, False), None)
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

    @contextlib.asynccontextmanager
    async def _card_lock(self, key: tuple[int, int]) -> AsyncIterator[None]:
        """Serialise card posting per message, so its images share one card.

        Without it, two images of one post finishing together would both see
        "no card yet" and post two. Per message rather than global so one busy
        campaign does not queue every other server's cards behind it. The lock
        entry is dropped once nobody holds or waits on it.
        """
        entry = self._card_locks.setdefault(key, _KeyedLock())
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
                del self._card_locks[key]

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
        self, event: VerdictEvent, cfg: GuildModConfig
    ) -> CardCleanup | None:
        """After a moderator confirmed, close and remove the uploader's other cards.

        One confirmation settles the whole campaign, so the other cards that
        account produced (one per message, across channels) are marked
        confirmed and deleted from the review channel rather than left for a
        moderator to click through one by one. Best-effort: a failure leaves
        the cards open, which is safe.
        """
        if event.confirmed_by is None or self._close_cards is None:
            return None
        try:
            cleanup = await self._close_cards(
                event.guild_id,
                event.uploader_id,
                event.message_id,
                event.confirmed_by,
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
        if cfg.safe_mode or event.matched_source != "guild" or not event.matched_hash_id:
            return None
        result = await self._execute(event, cfg, Action.DELETE, Decision.AUTO_ACT)
        if not result.success:
            return None
        detection_id = await self._audit(event, Action.DELETE.value, result)
        campaign.removed += 1
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
