"""Event-bus abstraction.

The :class:`Bus` protocol is the publish/consume surface every service is wired
against. Two implementations satisfy it: :class:`~optimus.bus.nats.EventBus`
(JetStream, for distributed mode) and
:class:`~optimus.bus.inprocess.InProcessBus` (asyncio queues, for single-process
"simple" mode). Services depend on the protocol, not a concrete transport, so the
composition layer chooses the backend without the service logic knowing which.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel

E = TypeVar("E", bound=BaseModel)


class Bus(Protocol):
    """The publish/consume surface shared by every bus backend.

    Only the two methods services actually call are part of the contract:
    :meth:`publish` (with the ``Nats-Msg-Id`` dedup semantics carried by
    ``msg_id``) and :meth:`consume` (a long-running consumer loop with bounded
    in-flight delivery). Backend-specific concerns (stream creation, payload
    validation, NATS draining) live on the concrete classes.
    """

    async def publish(self, subject: str, event: BaseModel, *, msg_id: str | None = None) -> None:
        """Publish ``event`` to ``subject``; a repeated ``msg_id`` is deduped."""
        ...

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
        max_inflight: int = 16,
        ack_wait: float = 60.0,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Run a consumer loop delivering ``subject`` events to ``handler``."""
        ...
