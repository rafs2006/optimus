"""Exponential backoff with full jitter."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Parameters for exponential backoff with full jitter.

    The delay for attempt ``n`` (0-indexed) is sampled uniformly from
    ``[0, min(max_delay, base * multiplier**n)]`` (AWS "full jitter").
    """

    base: float = 0.1
    multiplier: float = 2.0
    max_delay: float = 30.0
    max_attempts: int = 8

    def __post_init__(self) -> None:
        if self.base <= 0:
            raise ValueError("base must be positive")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")
        if self.max_delay < self.base:
            raise ValueError("max_delay must be >= base")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    def ceiling(self, attempt: int) -> float:
        """Return the un-jittered delay ceiling for a 0-indexed ``attempt``."""
        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        return min(self.max_delay, self.base * (self.multiplier**attempt))

    def delay(self, attempt: int, rng: random.Random | None = None) -> float:
        """Return a jittered delay (seconds) for a 0-indexed ``attempt``."""
        r = rng or random
        return r.uniform(0.0, self.ceiling(attempt))

    def delays(self, rng: random.Random | None = None) -> Iterator[float]:
        """Yield ``max_attempts`` jittered delays."""
        for attempt in range(self.max_attempts):
            yield self.delay(attempt, rng)


async def retry_async[T](
    func: Callable[[], Awaitable[T]],
    policy: BackoffPolicy | None = None,
    *,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    rng: random.Random | None = None,
) -> T:
    """Call ``func`` with retries governed by ``policy``.

    Re-raises the last exception once attempts are exhausted.
    """
    pol = policy or BackoffPolicy()
    last_exc: BaseException | None = None
    for attempt in range(pol.max_attempts):
        try:
            return await func()
        except retry_on as exc:
            last_exc = exc
            if attempt + 1 >= pol.max_attempts:
                break
            await asyncio.sleep(pol.delay(attempt, rng))
    assert last_exc is not None
    raise last_exc
