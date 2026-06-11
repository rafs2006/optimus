"""NATS JetStream helper: bounded streams, validated publish, pull-consumer loop.

Provides at-least-once delivery with explicit ack/nak. Streams are bounded with
a discard-old policy; dropped messages are counted via a Prometheus metric so
back-pressure is observable rather than silent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from prometheus_client import Counter
from pydantic import BaseModel, ValidationError

from optimus.contracts.events import EVENT_SUBJECTS, STREAM_EVENTS
from optimus.core.logging import correlation_context, get_logger

if TYPE_CHECKING:
    from nats.aio.msg import Msg
    from nats.js import JetStreamContext

_log = get_logger(__name__)

E = TypeVar("E", bound=BaseModel)

MESSAGES_PUBLISHED = Counter(
    "optimus_bus_messages_published_total",
    "Messages published to the bus.",
    ["subject"],
)
MESSAGES_ACKED = Counter(
    "optimus_bus_messages_acked_total",
    "Messages acknowledged by a consumer.",
    ["subject"],
)
MESSAGES_NAKED = Counter(
    "optimus_bus_messages_naked_total",
    "Messages negatively-acknowledged (will be redelivered).",
    ["subject"],
)
MESSAGES_DROPPED = Counter(
    "optimus_bus_messages_dropped_total",
    "Messages dropped (undecodable / poison).",
    ["subject", "reason"],
)

# Default per-stream bound: keep up to this many messages, oldest discarded.
DEFAULT_MAX_MSGS = 1_000_000
# ...and this many bytes (1 GiB) before discarding oldest.
DEFAULT_MAX_BYTES = 1 * 1024 * 1024 * 1024


class EventBus:
    """Thin wrapper over a JetStream context for publishing and consuming events."""

    def __init__(self, js: JetStreamContext) -> None:
        self._js = js

    @classmethod
    async def connect(cls, url: str) -> tuple[EventBus, Any]:
        """Connect to NATS and return an :class:`EventBus` plus the raw client.

        The caller owns the returned client and is responsible for draining it.
        """
        import nats as _nats

        nc = await _nats.connect(url)
        js = nc.jetstream()
        return cls(js), nc

    async def ensure_stream(
        self,
        name: str = STREAM_EVENTS,
        subjects: tuple[str, ...] = EVENT_SUBJECTS,
        *,
        max_msgs: int = DEFAULT_MAX_MSGS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """Create or update a bounded stream with a discard-old retention policy."""
        from nats.js.api import DiscardPolicy, RetentionPolicy, StreamConfig

        config = StreamConfig(
            name=name,
            subjects=list(subjects),
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.OLD,
            max_msgs=max_msgs,
            max_bytes=max_bytes,
        )
        try:
            await self._js.update_stream(config=config)
        except Exception:
            await self._js.add_stream(config=config)

    async def publish(self, subject: str, event: BaseModel) -> None:
        """Validate (by serialization) and publish ``event`` to ``subject``."""
        payload = event.model_dump_json().encode("utf-8")
        await self._js.publish(subject, payload)
        MESSAGES_PUBLISHED.labels(subject=subject).inc()

    async def consume(
        self,
        subject: str,
        durable: str,
        model: type[E],
        handler: Callable[[E], Awaitable[None]],
        *,
        batch: int = 16,
        fetch_timeout: float = 5.0,
        max_deliver: int = 5,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Run a pull-consumer loop with explicit ack/nak and poison-drop.

        Undecodable messages are terminated (dropped) and counted; handler
        failures are nak'd for redelivery up to ``max_deliver``.
        """
        from nats.js.api import AckPolicy, ConsumerConfig

        sub = await self._js.pull_subscribe(
            subject,
            durable=durable,
            config=ConsumerConfig(
                durable_name=durable,
                ack_policy=AckPolicy.EXPLICIT,
                max_deliver=max_deliver,
                ack_wait=30,
            ),
        )

        while stop_event is None or not stop_event.is_set():
            try:
                msgs = await sub.fetch(batch, timeout=fetch_timeout)
            except TimeoutError:
                continue
            except Exception:  # connection hiccup; let caller's supervisor restart
                _log.warning("bus_fetch_failed", subject=subject)
                await asyncio.sleep(0.5)
                continue
            for msg in msgs:
                await self._dispatch(subject, model, handler, msg)

    async def _dispatch(
        self,
        subject: str,
        model: type[E],
        handler: Callable[[E], Awaitable[None]],
        msg: Msg,
    ) -> None:
        try:
            event = model.model_validate_json(msg.data)
        except ValidationError:
            MESSAGES_DROPPED.labels(subject=subject, reason="decode").inc()
            _log.warning("bus_message_dropped", subject=subject, reason="decode")
            await msg.term()
            return

        cid = getattr(event, "correlation_id", None)
        with correlation_context(cid):
            try:
                await handler(event)
            except Exception:
                MESSAGES_NAKED.labels(subject=subject).inc()
                _log.exception("bus_handler_failed", subject=subject)
                await msg.nak()
                return
        MESSAGES_ACKED.labels(subject=subject).inc()
        await msg.ack()
