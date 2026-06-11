"""Tests for core utilities: backoff, circuit breaker, rate limiting, idempotency."""

from __future__ import annotations

import random

import pytest

from optimus.core.backoff import BackoffPolicy, retry_async
from optimus.core.circuit import CircuitBreaker, CircuitOpenError, CircuitState
from optimus.core.idempotency import IdempotencyGuard, build_key
from optimus.core.ratelimit import InMemoryRateLimiter, RateLimit

# --- backoff -------------------------------------------------------------------


def test_backoff_ceiling_is_capped() -> None:
    policy = BackoffPolicy(base=0.1, multiplier=2.0, max_delay=1.0)
    assert policy.ceiling(0) == pytest.approx(0.1)
    assert policy.ceiling(100) == 1.0


def test_backoff_delay_within_full_jitter_bounds() -> None:
    policy = BackoffPolicy(base=0.5, multiplier=2.0, max_delay=10.0)
    rng = random.Random(0)
    for attempt in range(5):
        delay = policy.delay(attempt, rng)
        assert 0.0 <= delay <= policy.ceiling(attempt)


def test_backoff_validation() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(base=0)
    with pytest.raises(ValueError):
        BackoffPolicy(multiplier=0.5)


async def test_retry_async_succeeds_after_failures() -> None:
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    policy = BackoffPolicy(base=0.001, max_delay=0.001, max_attempts=5)
    result = await retry_async(flaky, policy)
    assert result == "ok"
    assert attempts["n"] == 3


async def test_retry_async_reraises_after_exhaustion() -> None:
    async def always_fail() -> None:
        raise ValueError("nope")

    policy = BackoffPolicy(base=0.001, max_delay=0.001, max_attempts=2)
    with pytest.raises(ValueError):
        await retry_async(always_fail, policy)


# --- circuit breaker -----------------------------------------------------------


def test_circuit_opens_after_threshold() -> None:
    clock = {"t": 0.0}
    cb = CircuitBreaker(failure_threshold=3, recovery_time=5.0, time_source=lambda: clock["t"])
    assert cb.state is CircuitState.CLOSED
    for _ in range(3):
        cb.record_failure()
    assert cb.state is CircuitState.OPEN
    assert not cb.allow()


def test_circuit_half_open_then_closes_on_success() -> None:
    clock = {"t": 0.0}
    cb = CircuitBreaker(failure_threshold=1, recovery_time=5.0, time_source=lambda: clock["t"])
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    clock["t"] = 5.0
    assert cb.state is CircuitState.HALF_OPEN
    cb.record_success()
    assert cb.state is CircuitState.CLOSED


def test_circuit_half_open_reopens_on_failure() -> None:
    clock = {"t": 0.0}
    cb = CircuitBreaker(failure_threshold=1, recovery_time=5.0, time_source=lambda: clock["t"])
    cb.record_failure()
    clock["t"] = 5.0
    assert cb.state is CircuitState.HALF_OPEN
    cb.record_failure()
    assert cb.state is CircuitState.OPEN


async def test_circuit_call_fast_fails_when_open() -> None:
    cb = CircuitBreaker(failure_threshold=1, recovery_time=100.0)
    cb.record_failure()

    async def fn() -> int:
        return 1

    with pytest.raises(CircuitOpenError):
        await cb.call(fn)


# --- rate limiting -------------------------------------------------------------


async def test_in_memory_token_bucket_limits_and_refills() -> None:
    clock = {"t": 0.0}
    limiter = InMemoryRateLimiter(time_source=lambda: clock["t"])
    limit = RateLimit(capacity=2, refill_rate=1.0)
    assert await limiter.acquire("k", limit)
    assert await limiter.acquire("k", limit)
    assert not await limiter.acquire("k", limit)
    clock["t"] = 1.0  # one token refilled
    assert await limiter.acquire("k", limit)


async def test_rate_limit_keys_are_independent() -> None:
    limiter = InMemoryRateLimiter()
    limit = RateLimit(capacity=1, refill_rate=1.0)
    assert await limiter.acquire("a", limit)
    assert await limiter.acquire("b", limit)


async def test_in_memory_rate_limit_rejects_nonpositive_cost() -> None:
    limiter = InMemoryRateLimiter()
    limit = RateLimit(capacity=2, refill_rate=1.0)
    with pytest.raises(ValueError, match="cost must be positive"):
        await limiter.acquire("k", limit, cost=0)
    with pytest.raises(ValueError, match="cost must be positive"):
        await limiter.acquire("k", limit, cost=-1)


async def test_in_memory_rate_limit_evict_idle_bounds_memory() -> None:
    clock = {"t": 0.0}
    limiter = InMemoryRateLimiter(time_source=lambda: clock["t"])
    limit = RateLimit(capacity=2, refill_rate=1.0)
    for i in range(50):
        assert await limiter.acquire(f"key-{i}", limit)  # 1 token left each
    assert len(limiter._buckets) == 50
    # Nothing has refilled yet, so a sweep now frees nothing.
    assert limiter.evict_idle(limit) == 0
    assert len(limiter._buckets) == 50
    # After a full refill window every bucket is back at capacity and is freed.
    clock["t"] = 10.0
    assert limiter.evict_idle(limit) == 50
    assert len(limiter._buckets) == 0


async def test_evict_idle_keeps_actively_throttled_buckets() -> None:
    clock = {"t": 0.0}
    limiter = InMemoryRateLimiter(time_source=lambda: clock["t"])
    limit = RateLimit(capacity=3, refill_rate=1.0)
    assert await limiter.acquire("busy", limit)
    assert await limiter.acquire("busy", limit)
    assert await limiter.acquire("busy", limit)  # drained to 0
    clock["t"] = 1.0  # only 1 token back -> still below capacity
    assert limiter.evict_idle(limit) == 0
    assert "busy" in limiter._buckets


# --- idempotency ---------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool | None:
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0

    async def delete(self, key: str) -> int:
        return 1 if self._store.pop(key, None) is not None else 0


def test_build_key_format() -> None:
    assert build_key(11, 22) == "optimus:idem:11:22"


async def test_idempotency_guard_acquires_once() -> None:
    guard = IdempotencyGuard(_FakeRedis(), ttl_seconds=60)
    key = build_key(1, 2)
    assert await guard.acquire(key) is True
    assert await guard.acquire(key) is False
    assert await guard.seen(key) is True
    await guard.release(key)
    assert await guard.seen(key) is False
