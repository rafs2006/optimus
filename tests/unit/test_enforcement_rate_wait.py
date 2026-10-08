"""Enforcement waits for rate budget instead of leaving the scam for a moderator.

A scam account's burst once drained the per-server action bucket, and the next
image's ban came back ``rate_limited`` at once. Nothing retried it, the card
claimed "banned by user ID" next to the failed ban, and a moderator had to press
Confirm. These tests cover each part of the fix: the bounded wait, the
idempotency key that is no longer burned, Discord's ``retry_after``, the
coordinator's retry, the honest card text, the bigger burst and the
``message:`` hints.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis

from optimus.contracts.events import Action
from optimus.core.backoff import BackoffPolicy
from optimus.core.circuit import CircuitBreaker
from optimus.core.config import Settings
from optimus.core.ratelimit import RateLimit
from optimus.i18n import translate
from optimus.services.interactions.commands import COMMANDS
from optimus.services.moderation import actions as actions_mod
from optimus.services.moderation.actions import (
    MAX_RETRY_AFTER_SECONDS,
    ActionExecutor,
    ActionRequest,
)
from optimus.services.moderation.cooldown import Cooldown
from optimus.shared.failures import FailureKind, classify
from optimus.shared.outcomes import ActionResult, Step
from tests.unit.test_departed_ban_auto_close import _departed, _event, _Harness
from tests.unit.test_moderation_actions import _FakeRest


class _ScriptedLimiter:
    """Denies the first ``deny`` acquisitions, then allows every one."""

    def __init__(self, deny: int) -> None:
        self.deny = deny
        self.calls = 0

    async def acquire(self, key: str, limit: RateLimit, cost: float = 1.0) -> bool:
        self.calls += 1
        if self.deny > 0:
            self.deny -= 1
            return False
        return True


class _RateLimitedError(Exception):
    """Shaped like hikari's 429: a status and Discord's ``retry_after``."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("429")
        self.status = 429
        self.code = 0
        self.retry_after = retry_after


def _executor(
    limiter: object, *, wait: float = 0.0, refill: float = 100.0, redis: object | None = None
) -> tuple[ActionExecutor, _FakeRest]:
    from optimus.services.moderation.service import _ActionIdempotency

    redis = redis or fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    executor = ActionExecutor(
        rest,
        limiter,  # type: ignore[arg-type]
        bot_user_id=999,
        rate=RateLimit(capacity=1.0, refill_rate=refill),
        idempotency_acquire=_ActionIdempotency(redis).acquire,
        dm_cooldown=Cooldown(redis, window_seconds=3600),
        breaker=CircuitBreaker(),
        backoff=BackoffPolicy(base=0.001, max_delay=0.002, max_attempts=3),
        rate_wait_seconds=wait,
    )
    return executor, rest


def _req(key: str = "k1") -> ActionRequest:
    return ActionRequest(
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=42,
        action=Action.DELETE_BAN,
        idempotency_key=key,
    )


def _names(rest: _FakeRest) -> list[str]:
    return [name for name, _args in rest.calls]


# --- 1. Wait for a token instead of failing fast ---------------------------


async def test_an_empty_bucket_waits_for_a_token_and_then_bans() -> None:
    limiter = _ScriptedLimiter(deny=3)
    executor, rest = _executor(limiter, wait=2.0)
    result = await executor.execute(_req())
    assert result.success
    assert _names(rest) == ["delete_message", "ban_member"]
    assert limiter.calls == 4


async def test_the_wait_is_bounded_and_ends_rate_limited() -> None:
    executor, rest = _executor(_ScriptedLimiter(deny=10_000), wait=0.05)
    result = await executor.execute(_req())
    assert not result.success
    assert result.detail == "rate_limited"
    assert rest.calls == []


async def test_no_wait_configured_keeps_failing_fast() -> None:
    limiter = _ScriptedLimiter(deny=1)
    executor, _rest = _executor(limiter, wait=0.0)
    result = await executor.execute(_req())
    assert result.detail == "rate_limited"
    assert limiter.calls == 1


# --- 2. A rate-limited attempt no longer burns its idempotency key ---------


async def test_rate_limited_attempt_leaves_the_key_free_for_the_retry() -> None:
    limiter = _ScriptedLimiter(deny=1)
    executor, rest = _executor(limiter)
    first = await executor.execute(_req("same"))
    assert first.detail == "rate_limited"
    # The same action again: before the fix this was rejected as "duplicate".
    second = await executor.execute(_req("same"))
    assert second.success
    assert "ban_member" in _names(rest)
    # And a true replay is still caught.
    third = await executor.execute(_req("same"))
    assert third.detail == "duplicate"


# --- 3. Discord's retry_after is honoured ----------------------------------


def test_classify_keeps_discords_retry_after() -> None:
    failure = classify(_RateLimitedError(1.5))
    assert failure.kind is FailureKind.RATE_LIMITED
    assert failure.retry_after == 1.5
    assert failure.detail == "rate_limited"


def test_classify_without_retry_after_is_zero() -> None:
    exc = _RateLimitedError(0)
    del exc.retry_after
    assert classify(exc).retry_after == 0.0


async def test_a_429_sleeps_at_least_retry_after(monkeypatch: Any) -> None:
    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(actions_mod.asyncio, "sleep", _sleep)
    executor, rest = _executor(_ScriptedLimiter(deny=0))
    failures = [_RateLimitedError(0.75)]

    async def ban(*_a: Any, **_k: Any) -> None:
        if failures:
            raise failures.pop()

    rest.ban_member = ban  # type: ignore[method-assign]
    result = await executor.execute(_req())
    assert result.success
    assert sleeps and min(sleeps) >= 0.75


async def test_a_long_retry_after_is_not_slept_through_inline(monkeypatch: Any) -> None:
    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(actions_mod.asyncio, "sleep", _sleep)
    executor, rest = _executor(_ScriptedLimiter(deny=0))
    attempts = 0

    async def ban(*_a: Any, **_k: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise _RateLimitedError(MAX_RETRY_AFTER_SECONDS + 50)

    rest.ban_member = ban  # type: ignore[method-assign]
    result = await executor.execute(_req())
    assert not result.success
    assert result.detail == "rate_limited"
    assert attempts == 1
    assert sleeps == []


# --- 4. The coordinator retries an enforcement that ended rate_limited -----


def _flaky_execute(h: _Harness, fails: int) -> list[str]:
    real = h.coord._executor.execute
    keys: list[str] = []

    async def execute(req: ActionRequest) -> ActionResult:
        keys.append(req.idempotency_key)
        if len(keys) <= fails:
            return ActionResult(req.action, success=False, detail="rate_limited")
        return await real(req)

    h.coord._executor.execute = execute  # type: ignore[method-assign]
    return keys


async def test_rate_limited_auto_enforcement_is_retried_with_a_fresh_key() -> None:
    h = _Harness(target=_departed())
    h.coord._requeue_attempts = 2
    h.coord._requeue_delay = 0.0
    keys = _flaky_execute(h, fails=1)

    result = await h.coord.handle_verdict(_event())

    assert result.success
    assert len(keys) == 2
    assert keys[1] == f"{keys[0]}:r1"
    assert "ban_member" in h.rest.calls
    (card,) = h.reports
    assert card.auto_handled
    assert "failed" not in card.action_taken


async def test_retries_are_bounded() -> None:
    h = _Harness(target=_departed())
    h.coord._requeue_attempts = 2
    h.coord._requeue_delay = 0.0
    keys = _flaky_execute(h, fails=99)

    result = await h.coord.handle_verdict(_event())

    assert result.detail == "rate_limited"
    assert len(keys) == 3
    assert not h.reports[0].auto_handled


async def test_other_failures_are_not_retried() -> None:
    h = _Harness(target=_departed())
    h.coord._requeue_attempts = 2
    h.coord._requeue_delay = 0.0
    calls = 0

    async def execute(req: ActionRequest) -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(req.action, success=False, detail="missing_permissions:50013")

    h.coord._executor.execute = execute  # type: ignore[method-assign]
    await h.coord.handle_verdict(_event())
    assert calls == 1


async def test_a_step_level_429_is_retried_too() -> None:
    h = _Harness(target=_departed())
    h.coord._requeue_attempts = 1
    h.coord._requeue_delay = 0.0
    fails = [_RateLimitedError(MAX_RETRY_AFTER_SECONDS + 50)]

    async def ban(*_a: Any, **_k: Any) -> None:
        if fails:
            raise fails.pop()
        h.rest.calls.append("ban_member")

    h.rest.ban_member = ban  # type: ignore[method-assign]
    result = await h.coord.handle_verdict(_event())
    assert result.success
    assert "ban_member" in h.rest.calls


# --- 5. "banned by user ID" only when the ban happened ---------------------


async def test_failed_ban_does_not_claim_the_departed_uploader_was_banned() -> None:
    h = _Harness(target=_departed())

    async def execute(req: ActionRequest) -> ActionResult:
        return ActionResult(req.action, success=False, detail="rate_limited")

    h.coord._executor.execute = execute  # type: ignore[method-assign]
    await h.coord.handle_verdict(_event())
    (card,) = h.reports
    assert "delete_ban (failed: rate_limited)" in card.action_taken
    assert translate("report.boundary_departed_banned", "en") not in card.action_taken


async def test_refused_ban_step_does_not_claim_the_ban_either() -> None:
    h = _Harness(target=_departed())

    class _ForbiddenError(Exception):
        code = 50013
        status = 403

    async def ban(*_a: Any, **_k: Any) -> None:
        raise _ForbiddenError

    h.rest.ban_member = ban  # type: ignore[method-assign]
    result = await h.coord.handle_verdict(_event())
    assert any(s.step is Step.BAN and not s.success for s in result.steps)
    assert translate("report.boundary_departed_banned", "en") not in h.reports[0].action_taken


async def test_successful_ban_still_says_banned_by_user_id() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event())
    assert translate("report.boundary_departed_banned", "en") in h.reports[0].action_taken


# --- 6. Defaults sized for a multi-image burst -----------------------------


def test_defaults_fit_a_four_image_post_and_wait_and_retry() -> None:
    s = Settings()
    assert s.mod_action_rate_capacity >= 10
    assert s.mod_action_rate_wait_seconds > 0
    assert s.mod_action_requeue_attempts >= 1
    assert s.mod_action_requeue_delay_seconds > 0


# --- 7. The message: hint says where a bare ID works -----------------------


def test_message_option_hints_say_a_bare_id_needs_its_own_channel() -> None:
    (scamhash,) = (c for c in COMMANDS if c.name == "scamhash")
    hints = [
        opt.description
        for sub in scamhash.subcommands
        for opt in sub.options
        if opt.name == "message"
    ]
    assert hints
    assert all("own channel" in h for h in hints)
    assert all(len(h) <= 100 for h in hints)


def test_not_found_replies_explain_bare_ids_in_both_locales() -> None:
    for key in ("command.reviewmsg_not_found", "command.add_skip_message_not_found"):
        assert "link" in translate(key, "en")
        assert "channel where you run the command" in translate(key, "en")
        assert "kanalu u kome pokrećete komandu" in translate(key, "sr")
