"""Tests for the dependency readiness-probe factories."""

from __future__ import annotations

from optimus.core.readiness import nats_check, redis_check


class _FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.pinged = 0

    async def ping(self) -> bool:
        self.pinged += 1
        if self._fail:
            raise ConnectionError("redis down")
        return True


class _FakeNats:
    def __init__(self, *, connected: bool) -> None:
        self.is_connected = connected


async def test_redis_check_ready_when_ping_succeeds() -> None:
    redis = _FakeRedis()
    check = redis_check(redis)
    assert await check() is True
    assert redis.pinged == 1


async def test_redis_check_not_ready_when_ping_raises() -> None:
    check = redis_check(_FakeRedis(fail=True))
    assert await check() is False


async def test_redis_check_not_ready_when_client_missing() -> None:
    check = redis_check(None)
    assert await check() is False


async def test_redis_check_not_ready_for_non_pingable_object() -> None:
    check = redis_check(object())
    assert await check() is False


async def test_nats_check_reflects_connection_state() -> None:
    assert await nats_check(_FakeNats(connected=True))() is True
    assert await nats_check(_FakeNats(connected=False))() is False


async def test_nats_check_not_ready_when_attribute_absent() -> None:
    assert await nats_check(object())() is False
