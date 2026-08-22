"""Unit tests for the moderation coordinator orchestration."""

from __future__ import annotations

from datetime import UTC, datetime

import fakeredis.aioredis

from optimus.contracts.events import Action, Verdict, VerdictEvent
from optimus.core.backoff import BackoffPolicy
from optimus.core.circuit import CircuitBreaker
from optimus.core.ratelimit import InMemoryRateLimiter, RateLimit
from optimus.services.moderation.actions import ActionExecutor
from optimus.services.moderation.boundaries import TargetContext
from optimus.services.moderation.cooldown import Cooldown
from optimus.services.moderation.coordinator import GuildModConfig, ModerationCoordinator
from optimus.services.moderation.priority import PriorityDispatcher
from optimus.services.moderation.review import ReportData


class _FakeRest:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.dms: list[int] = []
        self.ban_purges: list[int] = []

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        self.calls.append("delete_message")

    async def timeout_member(self, guild_id: int, user_id: int, seconds: int) -> None:
        self.calls.append("timeout_member")

    async def kick_member(self, guild_id: int, user_id: int, reason: str) -> None:
        self.calls.append("kick_member")

    async def ban_member(
        self, guild_id: int, user_id: int, reason: str, purge_seconds: int = 0
    ) -> None:
        self.calls.append("ban_member")
        self.ban_purges.append(purge_seconds)

    async def unban_member(self, guild_id: int, user_id: int, reason: str) -> None:
        self.calls.append("unban_member")

    async def send_dm(self, user_id: int, content: str) -> None:
        self.dms.append(user_id)


def _event(
    *,
    verdict: Verdict = Verdict.SCAM,
    confidence: float = 0.95,
    source_url: str | None = None,
) -> VerdictEvent:
    return VerdictEvent(
        correlation_id="c",
        occurred_at=datetime.now(UTC),
        guild_id=1,
        channel_id=2,
        message_id=3,
        attachment_id=4,
        uploader_id=42,
        idempotency_key="idem-1",
        verdict=verdict,
        confidence=confidence,
        source_url=source_url,
    )


def _build(
    *,
    rest: _FakeRest,
    redis: object,
    cfg: GuildModConfig,
    target: TargetContext | None,
    reports: list[ReportData],
    audits: list[tuple[str, bool]],
    dispatcher: PriorityDispatcher | None = None,
    sweep: object | None = None,
) -> ModerationCoordinator:
    from optimus.services.moderation.service import _ActionIdempotency

    executor = ActionExecutor(
        rest,
        InMemoryRateLimiter(),
        bot_user_id=999,
        rate=RateLimit(capacity=10.0, refill_rate=0.001),
        idempotency_acquire=_ActionIdempotency(redis).acquire,
        dm_cooldown=Cooldown(redis, window_seconds=3600),
        breaker=CircuitBreaker(),
        backoff=BackoffPolicy(base=0.001, max_delay=0.002, max_attempts=2),
    )

    async def config(_gid: int) -> GuildModConfig:
        return cfg

    async def resolve_target(_gid: int, _uid: int) -> TargetContext | None:
        return target

    async def post(_chan: int, data: ReportData) -> int | None:
        reports.append(data)
        return 7

    async def audit(event: VerdictEvent, action: str, result: object) -> int | None:
        audits.append((action, result.success))  # type: ignore[attr-defined]
        return 7

    return ModerationCoordinator(
        config=config,
        target=resolve_target,
        executor=executor,
        report=post,
        audit=audit,
        dispatcher=dispatcher,
        sweep=sweep,  # type: ignore[arg-type]
    )


def _cfg(**kw: object) -> GuildModConfig:
    base: dict[str, object] = {
        "guild_id": 1,
        "configured_action": Action.DELETE_BAN,
        "mod_queue_threshold": 0.5,
        "auto_act_threshold": 0.85,
        "safe_mode": False,
        "review_channel_id": 100,
    }
    base.update(kw)
    return GuildModConfig(**base)  # type: ignore[arg-type]


def _target(**kw: object) -> TargetContext:
    base: dict[str, object] = {
        "user_id": 42,
        "guild_owner_id": 1,
        "bot_user_id": 999,
        "is_administrator": False,
        "top_role_position": 1,
        "bot_top_role_position": 5,
    }
    base.update(kw)
    return TargetContext(**base)  # type: ignore[arg-type]


async def test_clean_verdict_short_circuits() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest, redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=audits
    )
    result = await coord.handle_verdict(_event(verdict=Verdict.CLEAN))
    assert result.action is Action.NONE
    assert rest.calls == []
    assert audits == []


async def test_auto_act_ban_executes_and_audits_and_reports() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest, redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=audits
    )
    result = await coord.handle_verdict(_event())
    assert result.success
    assert "ban_member" in rest.calls
    assert audits == [("delete_ban", True)]
    assert len(reports) == 1
    assert reports[0].action_taken == "delete_ban"


async def test_boundary_refusal_downgrades_to_report() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    # Target is the guild owner -> punitive action refused.
    coord = _build(
        rest=rest,
        redis=redis,
        cfg=_cfg(),
        target=_target(user_id=42, guild_owner_id=42),
        reports=reports,
        audits=audits,
    )
    result = await coord.handle_verdict(_event())
    assert result.action is Action.REPORT_ONLY
    assert "ban_member" not in rest.calls
    assert audits == [("report_only", True)]


async def test_ban_carries_configured_purge_window() -> None:
    # The guild's ban_purge_seconds must flow from config through the request
    # into the REST call — that's what sweeps the scammer's other messages.
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    cfg = _cfg(ban_purge_seconds=86400)
    coord = _build(
        rest=rest, redis=redis, cfg=cfg, target=_target(), reports=reports, audits=audits
    )
    result = await coord.handle_verdict(_event())
    assert result.success
    assert "ban_member" in rest.calls
    assert rest.ban_purges == [86400]


async def test_missing_member_still_deletes_the_message() -> None:
    # The uploader left (or was already banned): the ban is impossible, but the
    # scam message itself must still be removed — not downgraded to report-only.
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(rest=rest, redis=redis, cfg=_cfg(), target=None, reports=reports, audits=audits)
    result = await coord.handle_verdict(_event())
    assert result.action is Action.DELETE
    assert result.success
    assert "delete_message" in rest.calls
    assert "ban_member" not in rest.calls
    assert audits == [("delete", True)]
    assert len(reports) == 1
    assert reports[0].action_taken == "delete"


async def test_failed_enforcement_is_reported_with_detail() -> None:
    # A crashing REST call must surface in the review-channel report, not just
    # in a silent audit row.
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()

    async def _boom(guild_id: int, user_id: int, reason: str, purge_seconds: int = 0) -> None:
        raise RuntimeError("discord exploded")

    rest.ban_member = _boom  # type: ignore[method-assign]
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest, redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=audits
    )
    result = await coord.handle_verdict(_event())
    assert not result.success
    assert audits == [("delete_ban", False)]
    assert len(reports) == 1
    assert reports[0].action_taken == "delete_ban (failed: unknown:RuntimeError)"


async def test_report_poster_failure_does_not_fail_the_verdict() -> None:
    # The action already ran and was audited; a review-channel posting failure
    # (missing permission, deleted channel) must not raise out of the handler —
    # a bus redelivery would only re-run the action into a "duplicate".
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    audits: list[tuple[str, bool]] = []
    from optimus.services.moderation.service import _ActionIdempotency

    executor = ActionExecutor(
        rest,
        InMemoryRateLimiter(),
        bot_user_id=999,
        rate=RateLimit(capacity=10.0, refill_rate=0.001),
        idempotency_acquire=_ActionIdempotency(redis).acquire,
        dm_cooldown=Cooldown(redis, window_seconds=3600),
        breaker=CircuitBreaker(),
        backoff=BackoffPolicy(base=0.001, max_delay=0.002, max_attempts=2),
    )

    async def config(_gid: int) -> GuildModConfig:
        return _cfg()

    async def resolve_target(_gid: int, _uid: int) -> TargetContext | None:
        return _target()

    async def post(_chan: int, _data: ReportData) -> int | None:
        raise RuntimeError("no send permission in review channel")

    async def audit(event: VerdictEvent, action: str, result: object) -> int | None:
        audits.append((action, result.success))  # type: ignore[attr-defined]
        return 7

    coord = ModerationCoordinator(
        config=config, target=resolve_target, executor=executor, report=post, audit=audit
    )
    result = await coord.handle_verdict(_event())
    assert result.success
    assert "ban_member" in rest.calls
    assert audits == [("delete_ban", True)]


async def test_queued_verdict_reports_without_action() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest, redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=audits
    )
    result = await coord.handle_verdict(_event(confidence=0.6))
    assert result.action is Action.REPORT_ONLY
    assert rest.calls == []
    assert audits == [("report_only", True)]
    assert len(reports) == 1


async def test_enforcement_runs_through_priority_dispatcher() -> None:
    # With a dispatcher wired, a protective auto-act still executes and audits
    # exactly as the direct path does — the dispatcher is transparent on success.
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    dispatcher: PriorityDispatcher = PriorityDispatcher(concurrency=2)
    await dispatcher.start()
    coord = _build(
        rest=rest,
        redis=redis,
        cfg=_cfg(),
        target=_target(),
        reports=reports,
        audits=audits,
        dispatcher=dispatcher,
    )
    result = await coord.handle_verdict(_event())
    await dispatcher.stop()

    assert result.success
    assert "ban_member" in rest.calls
    assert audits == [("delete_ban", True)]


async def test_dropped_dispatch_records_failure_audit() -> None:
    # When the dispatcher rejects a submission (full queue), enforcement returns
    # a success=False "dropped" result and the audit row is still recorded.
    from collections.abc import Awaitable, Callable

    from optimus.services.moderation.actions import ActionResult
    from optimus.services.moderation.priority import Priority, QueueFullError

    class _RejectingDispatcher(PriorityDispatcher[ActionResult]):
        async def submit(  # type: ignore[override]
            self,
            priority: Priority,
            factory: Callable[[], Awaitable[ActionResult]],
        ) -> object:
            raise QueueFullError(priority)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest,
        redis=redis,
        cfg=_cfg(),
        target=_target(),
        reports=reports,
        audits=audits,
        dispatcher=_RejectingDispatcher(),
    )
    result = await coord.handle_verdict(_event())

    assert not result.success
    assert result.detail == "dropped"
    assert rest.calls == []  # never reached the executor
    assert audits == [("delete_ban", False)]


async def test_safe_mode_blocks_auto_act() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest,
        redis=redis,
        cfg=_cfg(safe_mode=True),
        target=_target(),
        reports=reports,
        audits=audits,
    )
    result = await coord.handle_verdict(_event())
    assert result.action is Action.REPORT_ONLY
    assert "ban_member" not in rest.calls


async def test_global_match_verdict_only_queues_for_review() -> None:
    """A verdict matched from the *global* index must never auto-act, even at
    max confidence on a delete_ban guild -- it posts a review card flagged as
    a global match and touches nothing."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest, redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=audits
    )
    ev = _event(confidence=1.0)
    object.__setattr__(ev, "matched_source", "global")
    result = await coord.handle_verdict(ev)
    assert result.action is Action.REPORT_ONLY
    assert rest.calls == []  # no delete, no ban
    assert len(reports) == 1
    assert reports[0].global_match is True


async def test_guild_match_report_is_not_flagged_global() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest, redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=audits
    )
    ev = _event()
    object.__setattr__(ev, "matched_source", "guild")
    await coord.handle_verdict(ev)
    assert len(reports) == 1
    assert reports[0].global_match is False


async def test_sweep_runs_when_enforcement_succeeds() -> None:
    """A confirmed scam triggers the cross-channel campaign purge."""
    from optimus.services.moderation.sweep import SweepOutcome

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    swept: list[int] = []

    async def sweep(event: VerdictEvent) -> SweepOutcome:
        swept.append(event.uploader_id)
        return SweepOutcome(deleted=4, channels=4, harvested=("aa",))

    coord = _build(
        rest=rest,
        redis=redis,
        cfg=_cfg(),
        target=_target(),
        reports=reports,
        audits=audits,
        sweep=sweep,
    )

    result = await coord.handle_verdict(_event())

    assert result.success
    assert swept == [42]
    # The cleanup is surfaced to moderators, not silent.
    assert "purged 4 more in 4 channels" in reports[0].action_taken
    assert "+1 hashes blocklisted" in reports[0].action_taken


async def test_sweep_still_runs_when_the_ban_fails() -> None:
    """The reported incident: no ban means no native purge, so sweep anyway.

    If the sweep were gated on enforcement success, the exact failure that
    caused the incident (ban refused, Discord's ban purge therefore skipped)
    would also skip the cleanup -- leaving every other copy in place.
    """
    from optimus.services.moderation.sweep import SweepOutcome

    class _NoBanRest(_FakeRest):
        async def ban_member(
            self, guild_id: int, user_id: int, reason: str, purge_seconds: int = 0
        ) -> None:
            raise RuntimeError("Missing Permissions")

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    swept: list[int] = []

    async def sweep(event: VerdictEvent) -> SweepOutcome:
        swept.append(event.uploader_id)
        return SweepOutcome(deleted=3, channels=3)

    coord = _build(
        rest=_NoBanRest(),
        redis=redis,
        cfg=_cfg(),
        target=_target(),
        reports=reports,
        audits=audits,
        sweep=sweep,
    )

    result = await coord.handle_verdict(_event())

    assert result.success is False
    assert swept == [42]


async def test_sweep_failure_does_not_fail_the_verdict() -> None:
    """Cleanup is best-effort; the primary action already happened."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []

    async def sweep(_event: VerdictEvent) -> object:
        raise RuntimeError("db gone")

    coord = _build(
        rest=rest,
        redis=redis,
        cfg=_cfg(),
        target=_target(),
        reports=reports,
        audits=audits,
        sweep=sweep,
    )

    result = await coord.handle_verdict(_event())

    assert result.success
    assert rest.calls == ["delete_message", "ban_member"]


async def test_no_sweep_for_report_only_verdicts() -> None:
    """A queued/report-only verdict must not purge anything."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    swept: list[int] = []

    async def sweep(event: VerdictEvent) -> object:
        swept.append(event.uploader_id)
        raise AssertionError("must not be called")

    coord = _build(
        rest=rest,
        redis=redis,
        cfg=_cfg(safe_mode=True),
        target=_target(),
        reports=reports,
        audits=audits,
        sweep=sweep,
    )

    await coord.handle_verdict(_event())

    assert swept == []


# -- permission-aware reporting ----------------------------------------------


class _StubProbe:
    """Fixed permission answers, standing in for the gateway cache."""

    def __init__(self, *, channel: int | None, guild: int | None) -> None:
        self._channel = channel
        self._guild = guild

    async def channel_permissions(self, guild_id: int, channel_id: int) -> int | None:
        return self._channel

    async def guild_permissions(self, guild_id: int) -> int | None:
        return self._guild


async def test_report_names_the_permission_gap_that_blocked_the_cleanup() -> None:
    """The incident, end to end: the ban lands, the message stays, the card says so.

    Previously the review channel showed "delete_ban" with no hint that the
    message was still visible, so nobody knew a channel overwrite was the fix.
    """
    from optimus.services.moderation import permissions as perms

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest, redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=audits
    )
    coord.attach_permission_probe(_StubProbe(channel=0, guild=perms.BAN_MEMBERS))

    result = await coord.handle_verdict(_event())

    # The uploader is still removed -- that is what stops the campaign.
    assert rest.calls == ["ban_member"]
    assert result.partial is True
    assert len(reports) == 1
    assert reports[0].partial is True
    assert reports[0].problem is not None
    assert "View Channel" in reports[0].problem
    assert "<#2>" in reports[0].problem


async def test_report_carries_no_problem_when_enforcement_was_clean() -> None:
    from optimus.services.moderation import permissions as perms

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    reports: list[ReportData] = []
    coord = _build(
        rest=_FakeRest(), redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=[]
    )
    coord.attach_permission_probe(
        _StubProbe(channel=perms.DELETE_REQUIRES, guild=perms.BAN_MEMBERS)
    )

    result = await coord.handle_verdict(_event())

    assert result.success is True
    assert reports[0].problem is None
    assert reports[0].partial is False


async def test_campaign_sweep_still_runs_for_a_partially_applied_action() -> None:
    """A blocked delete must not cancel the purge of the uploader's other posts.

    The sweep is gated on the decision, not on the delete succeeding: other
    channels may well be accessible even when this one is not.
    """
    from optimus.services.moderation import permissions as perms

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    swept: list[int] = []

    async def _sweep(event: VerdictEvent) -> None:
        swept.append(event.uploader_id)
        return None

    coord = _build(
        rest=_FakeRest(),
        redis=redis,
        cfg=_cfg(),
        target=_target(),
        reports=[],
        audits=[],
        sweep=_sweep,
    )
    coord.attach_permission_probe(_StubProbe(channel=0, guild=perms.BAN_MEMBERS))

    await coord.handle_verdict(_event())

    assert swept == [42]


# --- Showing the image on the card, but only while it exists -------------------


async def test_the_card_shows_the_image_when_nothing_was_deleted() -> None:
    """A member report deletes nothing, so the reported image is still live."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest, redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=audits
    )

    await coord.handle_verdict(
        _event(verdict=Verdict.AMBIGUOUS, confidence=0.6, source_url="https://cdn/x.png")
    )

    assert "delete_message" not in rest.calls
    assert len(reports) == 1
    assert reports[0].image_url == "https://cdn/x.png"


async def test_the_card_drops_the_image_once_the_message_is_deleted() -> None:
    """The CDN url 404s after the delete; a broken image is worse than none."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest, redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=audits
    )

    result = await coord.handle_verdict(_event(source_url="https://cdn/x.png"))

    assert result.success and "delete_message" in rest.calls
    assert reports[0].image_url is None


async def test_a_refused_delete_still_shows_the_image() -> None:
    """The reported incident: the message survives, so the mod can still see it."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()

    async def _refuse(channel_id: int, message_id: int) -> None:
        raise RuntimeError("missing access")

    rest.delete_message = _refuse  # type: ignore[method-assign]
    reports: list[ReportData] = []
    audits: list[tuple[str, bool]] = []
    coord = _build(
        rest=rest, redis=redis, cfg=_cfg(), target=_target(), reports=reports, audits=audits
    )

    result = await coord.handle_verdict(_event(source_url="https://cdn/x.png"))

    assert result.message_deleted is False
    assert reports[0].image_url == "https://cdn/x.png"
