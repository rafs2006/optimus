"""Unit tests for action execution: idempotency, rate limit, breaker, DM cooldown."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from optimus.contracts.events import Action
from optimus.core.backoff import BackoffPolicy
from optimus.core.circuit import CircuitBreaker
from optimus.core.ratelimit import InMemoryRateLimiter, RateLimit
from optimus.services.moderation.actions import (
    ActionExecutor,
    ActionRequest,
    render_dm,
)
from optimus.services.moderation.cooldown import Cooldown


class _FakeRest:
    """Records calls; can be told to fail a given number of times."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.dms: list[tuple[int, str]] = []
        self._fail_times = fail_times

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("transient")
        self._record("delete_message", channel_id, message_id)

    async def timeout_member(self, guild_id: int, user_id: int, seconds: int) -> None:
        self._record("timeout_member", guild_id, user_id, seconds)

    async def kick_member(self, guild_id: int, user_id: int, reason: str) -> None:
        self._record("kick_member", guild_id, user_id)

    async def ban_member(
        self, guild_id: int, user_id: int, reason: str, purge_seconds: int = 0
    ) -> None:
        self._record("ban_member", guild_id, user_id)

    async def unban_member(self, guild_id: int, user_id: int, reason: str) -> None:
        self._record("unban_member", guild_id, user_id)

    async def send_dm(self, user_id: int, content: str) -> None:
        self.dms.append((user_id, content))


def _executor(
    rest: _FakeRest,
    *,
    redis: object,
    capacity: float = 5.0,
    breaker: CircuitBreaker | None = None,
) -> ActionExecutor:
    from optimus.services.moderation.service import _ActionIdempotency

    guard = _ActionIdempotency(redis)
    return ActionExecutor(
        rest,
        InMemoryRateLimiter(),
        bot_user_id=999,
        rate=RateLimit(capacity=capacity, refill_rate=0.001),
        idempotency_acquire=guard.acquire,
        dm_cooldown=Cooldown(redis, window_seconds=3600),
        breaker=breaker or CircuitBreaker(),
        backoff=BackoffPolicy(base=0.001, max_delay=0.002, max_attempts=3),
    )


def _req(action: Action = Action.DELETE_BAN, key: str = "k1") -> ActionRequest:
    return ActionRequest(
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=42,
        action=action,
        idempotency_key=key,
        guild_name="Test Guild",
    )


def test_render_dm_falls_back_to_english() -> None:
    msg = render_dm("xx", guild="Cool Server")
    assert "Cool Server" in msg


async def test_report_only_is_noop_success() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    result = await _executor(rest, redis=redis).execute(_req(Action.REPORT_ONLY))
    assert result.success
    assert result.detail == "no_enforcement"
    assert rest.calls == []


async def test_ban_deletes_and_bans_and_dms() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    result = await _executor(rest, redis=redis).execute(_req(Action.DELETE_BAN))
    assert result.success
    names = [c[0] for c in rest.calls]
    assert names == ["delete_message", "ban_member"]
    assert rest.dms == [(42, rest.dms[0][1])]


async def test_timeout_deletes_and_times_out_with_configured_seconds() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    req = ActionRequest(
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=42,
        action=Action.DELETE_TIMEOUT,
        idempotency_key="t1",
        timeout_seconds=600,
    )
    result = await _executor(rest, redis=redis).execute(req)
    assert result.success
    assert [c[0] for c in rest.calls] == ["delete_message", "timeout_member"]
    # The configured timeout is forwarded as the third positional arg.
    assert rest.calls[1] == ("timeout_member", (1, 42, 600))


async def test_kick_deletes_and_kicks() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    result = await _executor(rest, redis=redis).execute(_req(Action.DELETE_KICK, key="k"))
    assert result.success
    assert [c[0] for c in rest.calls] == ["delete_message", "kick_member"]


async def test_dm_failure_is_swallowed_and_action_still_succeeds() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    class _DmFailingRest(_FakeRest):
        async def send_dm(self, user_id: int, content: str) -> None:
            raise RuntimeError("recipient has DMs closed")

    rest = _DmFailingRest()
    # A closed-DM failure must not fail the enforcement action.
    result = await _executor(rest, redis=redis).execute(_req(Action.DELETE, key="dmfail"))
    assert result.success
    assert [c[0] for c in rest.calls] == ["delete_message"]
    assert rest.dms == []  # nothing recorded because send_dm raised


async def test_idempotency_blocks_duplicate() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    ex = _executor(rest, redis=redis)
    first = await ex.execute(_req(key="dup"))
    second = await ex.execute(_req(key="dup"))
    assert first.success
    assert not second.success
    assert second.detail == "duplicate"
    # Only one ban happened.
    assert [c[0] for c in rest.calls].count("ban_member") == 1


async def test_rate_limit_exhaustion_returns_failure() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    ex = _executor(rest, redis=redis, capacity=1.0)
    assert (await ex.execute(_req(key="a"))).success
    limited = await ex.execute(_req(key="b"))
    assert not limited.success
    assert limited.detail == "rate_limited"


async def test_backoff_recovers_from_transient_failure() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest(fail_times=1)
    result = await _executor(rest, redis=redis).execute(_req(Action.DELETE))
    assert result.success
    assert [c[0] for c in rest.calls] == ["delete_message"]


async def test_open_circuit_fails_fast() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest(fail_times=100)
    breaker = CircuitBreaker(failure_threshold=1, recovery_time=999.0)
    ex = _executor(rest, redis=redis, breaker=breaker)
    first = await ex.execute(_req(key="a"))
    assert not first.success
    second = await ex.execute(_req(key="b"))
    assert second.detail == "circuit_open"


async def test_injected_breaker_emits_transition_metrics() -> None:
    # A caller-supplied breaker must still drive the circuit-state metric, not
    # only the executor's default breaker.
    from optimus.services.moderation.actions import CIRCUIT_TRANSITIONS

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest(fail_times=100)
    breaker = CircuitBreaker(failure_threshold=1, recovery_time=999.0)
    before = CIRCUIT_TRANSITIONS.labels(from_state="closed", to_state="open")._value.get()

    ex = _executor(rest, redis=redis, breaker=breaker)
    await ex.execute(_req(key="trip"))  # one failure trips closed -> open

    after = CIRCUIT_TRANSITIONS.labels(from_state="closed", to_state="open")._value.get()
    assert after == before + 1


async def test_dm_cooldown_suppresses_second_warning() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    ex = _executor(rest, redis=redis)
    await ex.execute(_req(Action.DELETE, key="a"))
    await ex.execute(_req(Action.DELETE, key="b"))
    # Same uploader (42) -> only one DM within the cooldown window.
    assert len(rest.dms) == 1


async def test_no_dm_to_self() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest()
    ex = _executor(rest, redis=redis)
    req = ActionRequest(
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=999,
        action=Action.DELETE,
        idempotency_key="self",
    )
    await ex.execute(req)
    assert rest.dms == []


async def test_cooldown_window_validation() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with pytest.raises(ValueError, match="window_seconds"):
        Cooldown(redis, window_seconds=0)


async def _always_acquire(_key: str) -> bool:
    return True


async def test_default_breaker_records_transition_metric() -> None:
    from optimus.services.moderation.actions import CIRCUIT_TRANSITIONS

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _FakeRest(fail_times=99)
    # A default-constructed executor wires the metric/log observer onto its breaker.
    ex = ActionExecutor(
        rest,
        InMemoryRateLimiter(),
        bot_user_id=999,
        rate=RateLimit(capacity=50.0, refill_rate=0.001),
        idempotency_acquire=_always_acquire,
        dm_cooldown=Cooldown(redis, window_seconds=3600),
        backoff=BackoffPolicy(base=0.001, max_delay=0.002, max_attempts=1),
    )
    label = CIRCUIT_TRANSITIONS.labels(from_state="closed", to_state="open")
    before = label._value.get()
    for i in range(5):  # default failure_threshold (5) trips the breaker open
        await ex.execute(_req(key=f"trip{i}"))
    assert label._value.get() == before + 1


class _NotFoundError(Exception):
    """Stands in for hikari.NotFoundError (matched by class name)."""

    status = 404


class _ForbiddenError(Exception):
    """Stands in for hikari.ForbiddenError -- a missing Ban Members permission."""

    status = 403


class _BanFailsRest(_FakeRest):
    """Deletes fine, but the ban always fails -- the reported incident.

    Mirrors a guild where the bot has Manage Messages but not Ban Members.
    """

    def __init__(self) -> None:
        super().__init__()
        self.delete_attempts = 0

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        self.delete_attempts += 1
        if self.delete_attempts > 1:
            # Discord's real behaviour on re-deleting a removed message.
            raise _NotFoundError("Unknown Message")
        self._record("delete_message", channel_id, message_id)

    async def ban_member(
        self, guild_id: int, user_id: int, reason: str, purge_seconds: int = 0
    ) -> None:
        raise _ForbiddenError("Missing Permissions")


async def test_ban_failure_is_reported_not_masked_by_redelete() -> None:
    """The real ban error must survive the retry, not be replaced by a 404.

    Previously the retry re-ran the whole apply step, so attempt 2 re-deleted
    an already-deleted message and the resulting "Unknown Message" became the
    recorded failure -- hiding the actual missing-permission cause and making
    the incident look like a message-not-found problem.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _BanFailsRest()

    result = await _executor(rest, redis=redis).execute(_req(Action.DELETE_BAN))

    assert result.success is False
    # The recorded cause is the ban's 403, not the re-delete's 404.
    assert result.detail == "error:_ForbiddenError"
    assert "NotFound" not in result.detail
    # The delete was genuinely retried; it just no longer hijacks the error.
    assert rest.delete_attempts > 1


async def test_delete_of_already_removed_message_still_reaches_ban() -> None:
    """An already-gone message must not stop the punitive half of the action."""

    class _GoneRest(_FakeRest):
        async def delete_message(self, channel_id: int, message_id: int) -> None:
            raise _NotFoundError("Unknown Message")

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rest = _GoneRest()

    result = await _executor(rest, redis=redis).execute(_req(Action.DELETE_BAN))

    assert result.success
    assert [c[0] for c in rest.calls] == ["ban_member"]
