"""hikari gateway wiring: least-privilege intents, publish-only, (nearly) stateless.

The gateway subscribes to guild message **creation and updates**, applies
per-guild scan filters (ignored channels/roles, trusted users, bot/webhook
opt-in) using a Redis-cached guild config, and publishes one
``message_image.v1`` event per inspectable image. Updates matter for two
reasons: an attacker can post innocent text and *edit* the scam image in, and
Discord delivers link-unfurl embeds as a message update after the create. The
only state held beyond in-flight publishes is a small bounded cache of
already-published image URLs so repeated edits of one message do not re-fetch
the same image.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import hikari

from optimus.bus import Bus
from optimus.bus.nats import EventBus
from optimus.contracts.events import (
    SUBJECT_GUILD_JOINED,
    SUBJECT_MESSAGE_IMAGE,
    GuildJoinedEvent,
)
from optimus.core.config import Settings, get_settings
from optimus.core.guild_config import GuildConfig, GuildConfigCache
from optimus.core.health import HealthServer
from optimus.core.logging import configure_logging, correlation_context, get_logger
from optimus.core.readiness import nats_check, redis_check, shards_check
from optimus.services.gateway.extract import (
    Attachment,
    IncomingMessage,
    build_events,
    extract_image_urls_from_content,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    #: REST fallback for partial updates: ``(channel_id, message_id) -> Message``.
    FetchMessage = Callable[[int, int], Awaitable[hikari.Message]]


class HistoryReader(Protocol):
    """The REST surface a join-time history backfill needs.

    Kept behind a protocol so the backfill logic is unit-testable without a
    live hikari REST client (mirroring ``FetchMessage`` above).
    """

    async def list_text_channel_ids(self, guild_id: int) -> list[int]:
        """Ids of the guild's textable channels, in Discord's order."""
        ...

    async def fetch_recent_messages(
        self, channel_id: int, *, after: datetime, limit: int
    ) -> list[hikari.Message]:
        """Up to ``limit`` of the channel's *newest* messages created after ``after``."""
        ...


_log = get_logger(__name__)

# Least-privilege: read guild structure, message events, and message content.
GATEWAY_INTENTS = (
    hikari.Intents.GUILDS | hikari.Intents.GUILD_MESSAGES | hikari.Intents.MESSAGE_CONTENT
)


def _embed_image_urls(embeds: Iterable[hikari.Embed]) -> tuple[str, ...]:
    urls: list[str] = []
    for embed in embeds:
        if embed.image is not None and embed.image.url:
            urls.append(embed.image.url)
        if embed.thumbnail is not None and embed.thumbnail.url:
            urls.append(embed.thumbnail.url)
    return tuple(urls)


def shard_start_kwargs(settings: Settings) -> dict[str, object]:
    """Build the sharding kwargs for :meth:`hikari.GatewayBot.start`.

    Returns an empty dict when neither setting is configured so hikari keeps its
    automatic single-process behavior (zero change for small deployments). When
    ``shard_count`` is set the whole fleet agrees on the total; when
    ``shard_ids`` is also set this replica runs only that subset, enabling one
    process per shard subset. Validation of the relationship between the two
    lives in :class:`~optimus.core.config.Settings`.
    """
    kwargs: dict[str, object] = {}
    if settings.shard_count is not None:
        kwargs["shard_count"] = settings.shard_count
    if settings.shard_ids is not None:
        kwargs["shard_ids"] = list(settings.shard_ids)
    return kwargs


def _update_may_have_images(msg: hikari.PartialMessage) -> bool:
    """Cheap gate for partial updates: does this delta possibly carry an image?

    Fields Discord omitted from the update arrive as ``UNDEFINED`` and cannot
    contain new images. This runs on every edit in every guild, so it must not
    touch the network.
    """
    if msg.attachments is not hikari.UNDEFINED and len(msg.attachments) > 0:
        return True
    if msg.embeds is not hikari.UNDEFINED and len(_embed_image_urls(msg.embeds)) > 0:
        return True
    content = msg.content
    return isinstance(content, str) and bool(extract_image_urls_from_content(content))


def to_incoming_update(event: hikari.GuildMessageUpdateEvent) -> IncomingMessage | None:
    """Adapt a partial message-update event; ``None`` when the author is unknown.

    Update payloads only carry changed fields — anything else is ``UNDEFINED``
    and treated as empty. Link-unfurl updates typically omit the author
    entirely; those return ``None`` and the caller falls back to a REST fetch
    of the full message (enforcement needs a real uploader id to act on).
    """
    msg = event.message
    author = msg.author
    if author is hikari.UNDEFINED:
        return None
    attachments = (
        ()
        if msg.attachments is hikari.UNDEFINED
        else tuple(
            Attachment(
                id=int(a.id),
                url=str(a.url),
                filename=a.filename,
                content_type=a.media_type,
            )
            for a in msg.attachments
        )
    )
    embed_urls = () if msg.embeds is hikari.UNDEFINED else _embed_image_urls(msg.embeds)
    content = msg.content if isinstance(msg.content, str) else ""
    member = msg.member
    role_ids = (
        frozenset(int(r) for r in member.role_ids)
        if member is not hikari.UNDEFINED and member is not None
        else frozenset()
    )
    webhook_id = msg.webhook_id
    return IncomingMessage(
        guild_id=int(event.guild_id),
        channel_id=int(event.channel_id),
        message_id=int(msg.id),
        author_id=int(author.id),
        content=content,
        attachments=attachments,
        embed_image_urls=embed_urls,
        is_bot=bool(author.is_bot),
        is_webhook=webhook_id is not hikari.UNDEFINED and webhook_id is not None,
        author_role_ids=role_ids,
    )


def message_to_incoming(message: hikari.Message, *, guild_id: int) -> IncomingMessage:
    """Adapt a REST-fetched full message.

    REST messages carry no member object, so role-based ignore rules cannot be
    applied on this path; channel/user/bot/webhook filters still are.
    """
    attachments = tuple(
        Attachment(
            id=int(a.id),
            url=str(a.url),
            filename=a.filename,
            content_type=a.media_type,
        )
        for a in message.attachments
    )
    return IncomingMessage(
        guild_id=guild_id,
        channel_id=int(message.channel_id),
        message_id=int(message.id),
        author_id=int(message.author.id),
        content=message.content or "",
        attachments=attachments,
        embed_image_urls=_embed_image_urls(message.embeds),
        is_bot=bool(message.author.is_bot),
        is_webhook=message.webhook_id is not None,
        author_role_ids=frozenset(),
    )


def to_incoming(event: hikari.GuildMessageCreateEvent) -> IncomingMessage:
    """Adapt a hikari message-create event into a plain :class:`IncomingMessage`."""
    msg = event.message
    author = event.author
    attachments = tuple(
        Attachment(
            id=int(a.id),
            url=str(a.url),
            filename=a.filename,
            content_type=a.media_type,
        )
        for a in msg.attachments
    )
    member = msg.member
    role_ids = frozenset(int(r) for r in member.role_ids) if member is not None else frozenset()
    return IncomingMessage(
        guild_id=int(event.guild_id),
        channel_id=int(event.channel_id),
        message_id=int(msg.id),
        author_id=int(author.id),
        content=msg.content or "",
        attachments=attachments,
        embed_image_urls=_embed_image_urls(msg.embeds),
        is_bot=bool(author.is_bot),
        is_webhook=msg.webhook_id is not None,
        author_role_ids=role_ids,
    )


#: Bounded size of the (message_id, url) already-published cache. At ~120 bytes
#: an entry this is under 1 MiB, and old entries only matter while a message is
#: still plausibly being edited.
_SEEN_CACHE_MAX = 8192


class GatewayService:
    """Owns the hikari bot, the event bus, and the health server."""

    def __init__(
        self,
        settings: Settings,
        bus: Bus,
        config_cache: GuildConfigCache,
        health: HealthServer,
        *,
        fetch_message: FetchMessage | None = None,
        history: HistoryReader | None = None,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._config = config_cache
        self._health = health
        self._fetch_message = fetch_message
        self._history = history
        self._inflight: set[asyncio.Task[None]] = set()
        # LRU of already-published (message_id, image_url) pairs, so an edited
        # message only re-enters the pipeline for images we have not seen.
        self._seen: OrderedDict[tuple[int, str], None] = OrderedDict()

    async def on_message(self, event: hikari.GuildMessageCreateEvent) -> None:
        """Filter and publish image events for one message."""
        await self._scan(to_incoming(event), trigger="create")

    async def on_message_update(self, event: hikari.GuildMessageUpdateEvent) -> None:
        """Scan edited messages: only images not already published for this message.

        Covers the post-innocent-then-edit-scam-in bypass and late link-unfurl
        embeds. Partial updates that omit the author (typical for unfurls) are
        resolved with one REST fetch, and only when the delta actually carries
        image material.
        """
        msg = to_incoming_update(event)
        if msg is None:
            if self._fetch_message is None or not _update_may_have_images(event.message):
                return
            try:
                full = await self._fetch_message(int(event.channel_id), int(event.message.id))
            except Exception as exc:
                _log.warning(
                    "gateway_update_fetch_failed",
                    guild_id=int(event.guild_id),
                    message_id=int(event.message.id),
                    error=str(exc),
                )
                return
            msg = message_to_incoming(full, guild_id=int(event.guild_id))
        await self._scan(msg, trigger="update", only_unseen=True)

    async def _scan(self, msg: IncomingMessage, *, trigger: str, only_unseen: bool = False) -> None:
        config = await self._config.get(msg.guild_id)
        if not self._should_scan(config, msg):
            return
        with correlation_context() as cid:
            events = build_events(
                msg, correlation_id=cid, max_images=self._settings.gateway_max_attachments
            )
            if only_unseen:
                events = [e for e in events if (msg.message_id, e.url) not in self._seen]
            for image_event in events:
                self._mark_seen(msg.message_id, image_event.url)
                await self._bus.publish(SUBJECT_MESSAGE_IMAGE, image_event)
            if events:
                _log.info(
                    "gateway_published",
                    guild_id=msg.guild_id,
                    message_id=msg.message_id,
                    images=len(events),
                    trigger=trigger,
                )

    def _mark_seen(self, message_id: int, url: str) -> None:
        key = (message_id, url)
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > _SEEN_CACHE_MAX:
            self._seen.popitem(last=False)

    async def on_guild_join(self, event: hikari.GuildJoinEvent) -> None:
        """Publish a ``guild_joined.v1`` event so moderation can provision setup."""
        guild = event.guild
        with correlation_context() as cid:
            await self._bus.publish(
                SUBJECT_GUILD_JOINED,
                GuildJoinedEvent(
                    correlation_id=cid,
                    occurred_at=datetime.now(UTC),
                    guild_id=int(event.guild_id),
                    guild_name=guild.name if guild is not None else None,
                    owner_id=int(guild.owner_id) if guild is not None else None,
                ),
            )
        _log.info("gateway_guild_joined", guild_id=int(event.guild_id))
        if self._history is not None and self._settings.gateway_join_scan_days > 0:
            # Backfill in the background: joining a big guild must not block
            # the event listener. The task is tracked so drain() awaits it.
            self.track(asyncio.create_task(self._join_backfill(int(event.guild_id))))

    async def _join_backfill(self, guild_id: int) -> None:
        """Scan the last ``gateway_join_scan_days`` of history in a new guild.

        Mods usually install the bot *because* a scam wave is already underway,
        so the messages that motivated the install are in recent history, not
        the future. Reuses the exact live-scan path (per-guild filters, image
        extraction, publish) with ``only_unseen=True`` so a message that also
        arrives live is not published twice. Bounded by channel and per-channel
        message caps; a channel the bot cannot read (missing permission) is
        skipped rather than failing the whole backfill.
        """
        assert self._history is not None  # guarded by the caller
        after = datetime.now(UTC) - timedelta(days=self._settings.gateway_join_scan_days)
        try:
            channel_ids = await self._history.list_text_channel_ids(guild_id)
        except Exception as exc:
            _log.warning("join_backfill_channels_failed", guild_id=guild_id, error=str(exc))
            return
        channels_read = 0
        messages_scanned = 0
        for channel_id in channel_ids[: self._settings.gateway_join_scan_max_channels]:
            try:
                messages = await self._history.fetch_recent_messages(
                    channel_id,
                    after=after,
                    limit=self._settings.gateway_join_scan_messages_per_channel,
                )
            except Exception as exc:
                # Typical: no READ_MESSAGE_HISTORY / VIEW_CHANNEL in this channel.
                _log.info(
                    "join_backfill_channel_skipped",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    error=str(exc),
                )
                continue
            channels_read += 1
            for message in messages:
                await self._scan(
                    message_to_incoming(message, guild_id=guild_id),
                    trigger="join_backfill",
                    only_unseen=True,
                )
                messages_scanned += 1
        _log.info(
            "join_backfill_done",
            guild_id=guild_id,
            channels=channels_read,
            messages=messages_scanned,
            days=self._settings.gateway_join_scan_days,
        )

    @staticmethod
    def _should_scan(config: GuildConfig, msg: IncomingMessage) -> bool:
        return config.should_scan(
            channel_id=msg.channel_id,
            uploader_id=msg.author_id,
            author_role_ids=msg.author_role_ids,
            is_bot=msg.is_bot,
            is_webhook=msg.is_webhook,
        )

    def track(self, task: asyncio.Task[None]) -> None:
        """Track an in-flight publish task so shutdown can drain it."""
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def drain(self) -> None:
        """Await all in-flight publish tasks during graceful shutdown."""
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)


async def _amain() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, service_name="optimus-gateway")

    bus, nc = await EventBus.connect(settings.nats_url)
    await bus.ensure_stream()

    redis = _open_redis(settings)
    from optimus.db.engine import create_engine, create_session_factory, session_scope

    engine = create_engine()
    factory = create_session_factory(engine)

    def loader() -> object:
        return session_scope(factory)

    config_cache = GuildConfigCache(redis, loader)

    health = HealthServer(host=settings.health_host, port=settings.health_port)
    health.add_readiness_check(nats_check(nc), name="nats")
    if redis is not None:
        health.add_readiness_check(redis_check(redis), name="redis")
    await health.start()

    bot = hikari.GatewayBot(token=settings.discord_token, intents=GATEWAY_INTENTS)
    health.add_readiness_check(shards_check(bot), name="shards")

    async def _fetch(channel_id: int, message_id: int) -> hikari.Message:
        return await bot.rest.fetch_message(channel_id, message_id)

    service = GatewayService(settings, bus, config_cache, health, fetch_message=_fetch)

    @bot.listen(hikari.GuildMessageCreateEvent)
    async def _on_message(event: hikari.GuildMessageCreateEvent) -> None:
        await service.on_message(event)

    @bot.listen(hikari.GuildMessageUpdateEvent)
    async def _on_message_update(event: hikari.GuildMessageUpdateEvent) -> None:
        await service.on_message_update(event)

    @bot.listen(hikari.GuildJoinEvent)
    async def _on_guild_join(event: hikari.GuildJoinEvent) -> None:
        await service.on_guild_join(event)

    try:
        await bot.start(**shard_start_kwargs(settings))  # type: ignore[arg-type]
        await bot.join()
    finally:
        health.set_live(False)
        await service.drain()
        with contextlib.suppress(Exception):
            await bot.close()
        with contextlib.suppress(Exception):
            await nc.drain()
        await health.stop()
        await engine.dispose()


def _open_redis(settings: Settings) -> object | None:
    try:
        import redis.asyncio as aioredis

        return aioredis.from_url(settings.redis_url, decode_responses=True)
    except Exception:  # pragma: no cover - redis optional at boot
        _log.warning("redis_unavailable_gateway")
        return None


def main() -> None:
    """Console entrypoint: ``python -m optimus.services.gateway``."""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
