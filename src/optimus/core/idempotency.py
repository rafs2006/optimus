"""Idempotency guards backed by Redis ``SET NX``."""

from __future__ import annotations


def build_key(
    message_id: int | str, attachment_id: int | str, *, prefix: str = "optimus:idem"
) -> str:
    """Build the canonical idempotency key for a message attachment."""
    return f"{prefix}:{message_id}:{attachment_id}"


class IdempotencyGuard:
    """Single-acquire guard using Redis ``SET key value NX EX ttl``.

    :meth:`acquire` returns ``True`` exactly once per key within the TTL window,
    ensuring retries never double-act on the same attachment.
    """

    def __init__(self, redis: object, *, ttl_seconds: int = 86_400) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        self._redis = redis
        self._ttl = ttl_seconds

    async def acquire(self, key: str, token: str = "1") -> bool:  # noqa: S107 - sentinel value, not a credential
        """Atomically claim ``key``; return whether this caller won the claim."""
        result = await self._redis.set(  # type: ignore[attr-defined]
            key, token, nx=True, ex=self._ttl
        )
        return result is True or result == "OK"

    async def seen(self, key: str) -> bool:
        """Whether ``key`` has already been claimed."""
        exists = await self._redis.exists(key)  # type: ignore[attr-defined]
        return bool(exists)

    async def release(self, key: str) -> None:
        """Release a claim (e.g. to allow reprocessing after a failure)."""
        await self._redis.delete(key)  # type: ignore[attr-defined]
