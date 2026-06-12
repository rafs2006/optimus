"""Behavioural tests for :class:`~optimus.bus.inprocess.InProcessBus`.

These prove the in-process bus mirrors the three JetStream behaviours simple
mode depends on, without any NATS:

* **publish dedup** — a repeated ``msg_id`` within the window is dropped before
  fan-out (and expires from the window on an injected clock);
* **fan-out** — one publish reaches every durable subscribed to the subject;
* **bounded in-flight** — a slow handler never runs more than ``max_inflight``
  concurrently even under a burst;
* **redelivery + poison-drop** — a handler that raises is retried up to
  ``max_deliver`` and then dropped, while a success is delivered exactly once.

The consume loop runs as a background task driven by a ``stop_event``; each test
publishes, waits for the expected handler calls, then stops the loop.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticSerializationError

from optimus.bus.inprocess import InProcessBus


class _Evt(BaseModel):
    correlation_id: str = "c"
    n: int = 0


async def _run_until(predicate, within: float = 1.0) -> None:  # type: ignore[no-untyped-def]
    """Poll ``predicate`` until true or ``within`` seconds elapse."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")


async def _consume_in_task(bus: InProcessBus, stop: asyncio.Event, **kwargs) -> asyncio.Task[None]:  # type: ignore[no-untyped-def]
    task = asyncio.create_task(bus.consume(stop_event=stop, **kwargs))
    # Yield so the consumer registers before the first publish.
    await asyncio.sleep(0)
    return task


async def _drain(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


class _Clock:
    """A manually advanced monotonic clock for dedup-window assertions."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def test_publish_fans_out_to_every_consumer() -> None:
    bus = InProcessBus()
    a: list[int] = []
    b: list[int] = []

    async def handler_a(evt: _Evt) -> None:
        a.append(evt.n)

    async def handler_b(evt: _Evt) -> None:
        b.append(evt.n)

    stop = asyncio.Event()
    t1 = await _consume_in_task(bus, stop, subject="s", durable="a", model=_Evt, handler=handler_a)
    t2 = await _consume_in_task(bus, stop, subject="s", durable="b", model=_Evt, handler=handler_b)

    await bus.publish("s", _Evt(n=1))
    await _run_until(lambda: a == [1] and b == [1])

    await _drain(stop, t1)
    await _drain(stop, t2)


async def test_duplicate_msg_id_dropped_within_window() -> None:
    clock = _Clock()
    bus = InProcessBus(duplicate_window=100.0, time_source=clock)
    seen: list[int] = []

    async def handler(evt: _Evt) -> None:
        seen.append(evt.n)

    stop = asyncio.Event()
    task = await _consume_in_task(bus, stop, subject="s", durable="d", model=_Evt, handler=handler)

    await bus.publish("s", _Evt(n=1), msg_id="dup")
    await bus.publish("s", _Evt(n=2), msg_id="dup")  # dropped: same id, same window
    await _run_until(lambda: seen == [1])
    # Give a duplicate a chance to (wrongly) arrive before asserting it did not.
    await asyncio.sleep(0.02)
    assert seen == [1]

    # Past the window the same id is accepted again.
    clock.t = 200.0
    await bus.publish("s", _Evt(n=3), msg_id="dup")
    await _run_until(lambda: seen == [1, 3])

    await _drain(stop, task)


async def test_distinct_msg_ids_both_delivered() -> None:
    bus = InProcessBus()
    seen: list[int] = []

    async def handler(evt: _Evt) -> None:
        seen.append(evt.n)

    stop = asyncio.Event()
    task = await _consume_in_task(bus, stop, subject="s", durable="d", model=_Evt, handler=handler)

    await bus.publish("s", _Evt(n=1), msg_id="a")
    await bus.publish("s", _Evt(n=2), msg_id="b")
    await _run_until(lambda: sorted(seen) == [1, 2])

    await _drain(stop, task)


async def test_inflight_bounded_by_max_inflight() -> None:
    bus = InProcessBus()
    concurrent = 0
    peak = 0
    release = asyncio.Event()

    async def handler(_evt: _Evt) -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await release.wait()
        concurrent -= 1

    stop = asyncio.Event()
    task = await _consume_in_task(
        bus, stop, subject="s", durable="d", model=_Evt, handler=handler, max_inflight=2
    )

    # Publish a burst larger than the bound. The queue is sized to the bound, so
    # the extra publishes block until a slot frees — start them concurrently.
    publishers = [asyncio.create_task(bus.publish("s", _Evt(n=i))) for i in range(6)]
    await _run_until(lambda: concurrent == 2)
    await asyncio.sleep(0.02)
    assert peak == 2  # never more than max_inflight ran at once

    release.set()
    await asyncio.gather(*publishers)
    await _drain(stop, task)
    assert peak == 2


async def test_handler_failure_redelivers_then_drops() -> None:
    bus = InProcessBus()
    attempts = 0

    async def handler(_evt: _Evt) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    stop = asyncio.Event()
    task = await _consume_in_task(
        bus, stop, subject="s", durable="d", model=_Evt, handler=handler, max_deliver=3
    )

    await bus.publish("s", _Evt(n=1))
    await _run_until(lambda: attempts == 3)
    # After max_deliver the message is dropped — no further attempts.
    await asyncio.sleep(0.05)
    assert attempts == 3

    await _drain(stop, task)


async def test_successful_handler_delivered_once() -> None:
    bus = InProcessBus()
    count = 0

    async def handler(_evt: _Evt) -> None:
        nonlocal count
        count += 1

    stop = asyncio.Event()
    task = await _consume_in_task(bus, stop, subject="s", durable="d", model=_Evt, handler=handler)

    await bus.publish("s", _Evt(n=1))
    await _run_until(lambda: count == 1)
    await asyncio.sleep(0.02)
    assert count == 1

    await _drain(stop, task)


async def test_unserializable_event_rejected_at_publish() -> None:
    bus = InProcessBus()

    class _Bad(BaseModel):
        value: object = object()  # not JSON-serializable

    with pytest.raises(PydanticSerializationError):
        await bus.publish("s", _Bad())
