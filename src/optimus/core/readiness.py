"""Readiness-probe factories for the dependencies services share.

Each factory returns an async predicate suitable for
:meth:`optimus.core.health.HealthServer.add_readiness_check`. Probes never
raise: a failed dependency resolves to ``False`` so ``/readyz`` reports 503
rather than crashing the probe handler.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

ReadinessCheck = Callable[[], Awaitable[bool]]


@runtime_checkable
class _Pingable(Protocol):
    def ping(self) -> Awaitable[Any]: ...


def redis_check(redis: object | None) -> ReadinessCheck:
    """Return a probe that is ready when ``redis`` answers ``PING``.

    A ``None`` client (Redis optional at boot) is treated as not ready.
    """

    async def _check() -> bool:
        if not isinstance(redis, _Pingable):
            return False
        try:
            await redis.ping()
        except Exception:
            return False
        return True

    return _check


def nats_check(nc: object) -> ReadinessCheck:
    """Return a probe that is ready while the NATS client reports connected."""

    async def _check() -> bool:
        return bool(getattr(nc, "is_connected", False))

    return _check
