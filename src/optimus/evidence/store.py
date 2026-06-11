"""Opt-in, encrypted, TTL'd evidence storage on an S3-compatible backend.

Evidence is sensitive, so storage is **off by default** and per-guild opt-in.
Objects are written with server-side encryption (SSE), keyed as
``evidence/{guild_id}/{detection_id}``, and given a short TTL (default 1h, hard
cap 24h). Retrieval is via a single short-lived presigned ``GET`` URL. A janitor
deletes expired objects.

The S3 surface is abstracted behind :class:`ObjectStore` so the TTL math and key
scheme are testable without network or credentials. The aioboto3-backed
implementation lives in :class:`S3ObjectStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

#: Hard upper bound on evidence retention regardless of caller request.
MAX_TTL_SECONDS = 24 * 3600


def object_key(guild_id: int, detection_id: int) -> str:
    """The canonical object key for a detection's evidence."""
    return f"evidence/{guild_id}/{detection_id}"


def clamp_ttl(ttl_seconds: int, *, max_ttl: int = MAX_TTL_SECONDS) -> int:
    """Clamp a requested TTL to ``[1, max_ttl]``."""
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be >= 1")
    return min(ttl_seconds, max_ttl)


def expiry_at(
    ttl_seconds: int, *, now: datetime | None = None, max_ttl: int = MAX_TTL_SECONDS
) -> datetime:
    """Compute the absolute expiry for a clamped TTL."""
    base = now or datetime.now(UTC)
    return base + timedelta(seconds=clamp_ttl(ttl_seconds, max_ttl=max_ttl))


class ObjectStore(Protocol):
    """Minimal object-store surface the evidence store depends on."""

    async def put(self, key: str, data: bytes, *, content_type: str, sse: str) -> None: ...

    async def presign_get(self, key: str, *, expires_in: int) -> str: ...

    async def delete(self, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class StoredEvidence:
    """The result of storing one piece of evidence."""

    object_key: str
    expires_at: datetime
    presigned_url: str


class EvidenceStore:
    """Stores and retrieves encrypted, TTL'd evidence objects."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        sse: str = "AES256",
        default_ttl: int = 3600,
        max_ttl: int = MAX_TTL_SECONDS,
        presign_seconds: int = 300,
    ) -> None:
        self._store = store
        self._sse = sse
        self._default_ttl = default_ttl
        self._max_ttl = max_ttl
        self._presign_seconds = presign_seconds

    async def store(
        self,
        *,
        guild_id: int,
        detection_id: int,
        data: bytes,
        content_type: str = "application/octet-stream",
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> StoredEvidence:
        """Encrypt-and-store ``data`` and return its key, expiry, and access URL."""
        key = object_key(guild_id, detection_id)
        ttl = clamp_ttl(ttl_seconds or self._default_ttl, max_ttl=self._max_ttl)
        await self._store.put(key, data, content_type=content_type, sse=self._sse)
        url = await self._store.presign_get(key, expires_in=self._presign_seconds)
        return StoredEvidence(
            object_key=key,
            expires_at=expiry_at(ttl, now=now, max_ttl=self._max_ttl),
            presigned_url=url,
        )

    async def presigned_url(self, key: str) -> str:
        """Generate a fresh short-lived presigned GET URL for an existing object."""
        return await self._store.presign_get(key, expires_in=self._presign_seconds)

    async def delete(self, key: str) -> None:
        """Delete an evidence object (used by the janitor)."""
        await self._store.delete(key)


class S3ObjectStore:
    """aioboto3-backed :class:`ObjectStore` for an S3-compatible endpoint."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str = "",
        region: str = "us-east-1",
    ) -> None:
        self._bucket = bucket
        self._endpoint_url = endpoint_url or None
        self._region = region

    def _session(self) -> object:  # pragma: no cover - thin aioboto3 wrapper
        import aioboto3

        return aioboto3.Session()

    def _client(self) -> object:  # pragma: no cover - thin aioboto3 wrapper
        return self._session().client(  # type: ignore[attr-defined]
            "s3", endpoint_url=self._endpoint_url, region_name=self._region
        )

    async def put(  # pragma: no cover
        self, key: str, data: bytes, *, content_type: str, sse: str
    ) -> None:
        async with self._client() as client:  # type: ignore[attr-defined]
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                ServerSideEncryption=sse,
            )

    async def presign_get(self, key: str, *, expires_in: int) -> str:  # pragma: no cover
        async with self._client() as client:  # type: ignore[attr-defined]
            url: str = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
            return url

    async def delete(self, key: str) -> None:  # pragma: no cover
        async with self._client() as client:  # type: ignore[attr-defined]
            await client.delete_object(Bucket=self._bucket, Key=key)
