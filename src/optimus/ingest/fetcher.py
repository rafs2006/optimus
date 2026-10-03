"""Streaming, SSRF-hardened image fetcher built on aiohttp.

Connections are made to the IP pinned by :mod:`optimus.ingest.ssrf` (DNS is
resolved once, up front) while the original host is preserved for TLS SNI and
the ``Host`` header. Redirects are followed manually so each hop is re-validated
through the guard. The body is read in bounded chunks and the fetch is aborted
the moment the size cap is exceeded, so an oversized response never lands fully
in memory. Raw bytes are returned to the caller and never written to disk.
"""

from __future__ import annotations

import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

from optimus.core.logging import get_logger
from optimus.ingest.ssrf import (
    ALLOWED_CONTENT_TYPES,
    PinnedTarget,
    SSRFError,
    validate_url,
)

_log = get_logger(__name__)

# Magic-byte signatures for the formats we accept. (offset, signature) pairs;
# WebP additionally checks the "WEBP" tag at offset 8.
_MAGIC: tuple[tuple[int, bytes], ...] = (
    (0, b"\x89PNG\r\n\x1a\n"),  # PNG
    (0, b"\xff\xd8\xff"),  # JPEG
    (0, b"GIF87a"),  # GIF
    (0, b"GIF89a"),  # GIF
    (0, b"BM"),  # BMP
)


class FetchError(Exception):
    """Raised when an image cannot be safely fetched or validated."""


@dataclass(frozen=True, slots=True)
class FetchedImage:
    """A fetched, size- and type-validated image."""

    data: bytes
    content_type: str
    final_url: str


def sniff_content_type(data: bytes) -> str | None:
    """Return a normalized content type from magic bytes, or ``None``."""
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for offset, sig in _MAGIC:
        if data[offset : offset + len(sig)] == sig:
            if sig.startswith(b"\x89PNG"):
                return "image/png"
            if sig.startswith(b"\xff\xd8"):
                return "image/jpeg"
            if sig.startswith(b"GIF"):
                return "image/gif"
            if sig.startswith(b"BM"):
                return "image/bmp"
    return None


def _build_connector(target: PinnedTarget) -> aiohttp.TCPConnector:
    """Build a connector that pins ``target.host`` to its resolved IP."""
    resolved: list[ResolveResult] = [
        ResolveResult(
            hostname=target.host,
            host=target.ip,
            port=target.port,
            family=target.family,
            proto=0,
            flags=0,
        )
    ]
    return aiohttp.TCPConnector(resolver=_StaticResolver(resolved), ttl_dns_cache=0)


class _StaticResolver(AbstractResolver):
    """An aiohttp resolver that returns a fixed, pre-validated address."""

    def __init__(self, hosts: list[ResolveResult]) -> None:
        self._hosts = hosts

    async def resolve(self, host: str, port: int = 0, family: int = 0) -> list[ResolveResult]:
        return self._hosts

    async def close(self) -> None:
        return None


async def _fetch_bounded[T](
    url: str,
    *,
    accept: str,
    read: Callable[[aiohttp.ClientResponse], Awaitable[T]],
    max_redirects: int,
    total_timeout: float,
) -> T:
    """Shared SSRF-guarded GET: pinned IP, manual re-validated redirects.

    ``read`` consumes the final 200 response and owns every body check (size
    cap, content type), so each public fetcher keeps its own acceptance rules
    while the connection/redirect policy stays in exactly one place.
    """
    timeout = aiohttp.ClientTimeout(total=total_timeout)
    current = url
    seen = 0
    while True:
        target = validate_url(current)
        connector = _build_connector(target)
        ssl_ctx: ssl.SSLContext | bool = (
            ssl.create_default_context() if target.scheme == "https" else False
        )
        try:
            async with (
                aiohttp.ClientSession(connector=connector, timeout=timeout) as session,
                session.get(
                    current,
                    allow_redirects=False,
                    ssl=ssl_ctx,
                    headers={"Accept": accept},
                ) as resp,
            ):
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise FetchError("redirect without Location header")
                    seen += 1
                    if seen > max_redirects:
                        raise FetchError("too many redirects")
                    current = str(resp.url.join(URL(location)))
                    continue
                if resp.status != 200:
                    raise FetchError(f"unexpected status {resp.status}")
                return await read(resp)
        except aiohttp.ClientError as exc:
            raise FetchError(f"transport error: {exc}") from exc


async def fetch_image(
    url: str,
    *,
    max_bytes: int,
    max_redirects: int = 3,
    total_timeout: float = 15.0,
) -> FetchedImage:
    """Fetch and validate the image at ``url``.

    Raises :class:`FetchError` (or :class:`SSRFError`) on any policy violation:
    blocked address, disallowed scheme, too many redirects, oversize body, or a
    content type that fails either the header allowlist or magic-byte sniff.
    """
    return await _fetch_bounded(
        url,
        accept="image/*",
        read=partial(_read_validated, max_bytes=max_bytes),
        max_redirects=max_redirects,
        total_timeout=total_timeout,
    )


#: Content types a ``/scamhash import`` upload may arrive with. Discord labels
#: a ``.json`` upload ``application/json``; a renamed or hand-saved file can
#: come through as plain text or a generic binary type. An empty header is
#: also accepted -- the JSON parser is the real gate, this only turns away
#: things that are plainly not a document (an HTML page, an image).
TEXT_CONTENT_TYPES = frozenset({"application/json", "text/plain", "application/octet-stream"})


async def fetch_text(
    url: str,
    *,
    max_bytes: int,
    max_redirects: int = 3,
    total_timeout: float = 15.0,
) -> bytes:
    """Fetch a small text document (an import file) under the same guards.

    Returns the raw bytes, undecoded: ``json.loads`` on bytes detects UTF-8
    with or without a byte-order mark, which a Windows editor may add.
    """
    return await _fetch_bounded(
        url,
        accept="application/json, text/plain",
        read=partial(_read_text, max_bytes=max_bytes),
        max_redirects=max_redirects,
        total_timeout=total_timeout,
    )


async def _read_capped(resp: aiohttp.ClientResponse, *, max_bytes: int) -> bytes:
    """Stream the body under a hard size cap, aborting the moment it is exceeded."""
    declared = resp.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise FetchError("content-length exceeds cap")
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            resp.close()  # abort mid-stream; do not buffer the rest
            raise FetchError("body exceeds size cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _header_content_type(resp: aiohttp.ClientResponse) -> str:
    return (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()


async def _read_text(resp: aiohttp.ClientResponse, *, max_bytes: int) -> bytes:
    header_ct = _header_content_type(resp)
    if header_ct and header_ct not in TEXT_CONTENT_TYPES:
        raise FetchError(f"disallowed content type: {header_ct!r}")
    data = await _read_capped(resp, max_bytes=max_bytes)
    if sniff_content_type(data) is not None:
        raise FetchError("expected a text document, got an image")
    return data


async def _read_validated(resp: aiohttp.ClientResponse, *, max_bytes: int) -> FetchedImage:
    """Stream the body under a hard size cap and validate the content type."""
    header_ct = _header_content_type(resp)
    if header_ct and header_ct not in ALLOWED_CONTENT_TYPES:
        raise FetchError(f"disallowed content type: {header_ct!r}")
    data = await _read_capped(resp, max_bytes=max_bytes)
    sniffed = sniff_content_type(data)
    if sniffed is None:
        raise FetchError("content failed magic-byte validation")
    return FetchedImage(data=data, content_type=sniffed, final_url=str(resp.url))


__all__ = [
    "TEXT_CONTENT_TYPES",
    "FetchError",
    "FetchedImage",
    "SSRFError",
    "fetch_image",
    "fetch_text",
    "sniff_content_type",
]
