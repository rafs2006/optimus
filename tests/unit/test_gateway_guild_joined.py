"""Unit test: the gateway publishes guild_joined.v1 on a guild-join event."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

from optimus.contracts.events import (
    SUBJECT_GUILD_JOINED,
    SUBJECT_MESSAGE_IMAGE,
    GuildJoinedEvent,
)
from optimus.core.guild_config import GuildConfig, GuildConfigCache
from optimus.services.gateway.bot import GatewayService


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    async def publish(self, subject: str, event: object) -> None:
        self.published.append((subject, event))


@dataclass
class _Guild:
    name: str
    owner_id: int


@dataclass
class _JoinEvent:
    guild_id: int
    guild: _Guild | None


async def test_on_guild_join_publishes_event() -> None:
    bus = _FakeBus()
    service = GatewayService(
        settings=object(),  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        config_cache=GuildConfigCache(None, lambda: None),
        health=object(),  # type: ignore[arg-type]
    )
    event = _JoinEvent(guild_id=123, guild=_Guild(name="My Server", owner_id=7))
    await service.on_guild_join(event)  # type: ignore[arg-type]

    assert len(bus.published) == 1
    subject, published = bus.published[0]
    assert subject == SUBJECT_GUILD_JOINED
    assert isinstance(published, GuildJoinedEvent)
    assert published.guild_id == 123
    assert published.guild_name == "My Server"
    assert published.owner_id == 7


async def test_on_guild_join_handles_missing_guild() -> None:
    bus = _FakeBus()
    service = GatewayService(
        settings=object(),  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        config_cache=GuildConfigCache(None, lambda: None),
        health=object(),  # type: ignore[arg-type]
    )
    await service.on_guild_join(_JoinEvent(guild_id=99, guild=None))  # type: ignore[arg-type]
    _, published = bus.published[0]
    assert isinstance(published, GuildJoinedEvent)
    assert published.guild_name is None
    assert published.owner_id is None


# --- join-time history backfill --------------------------------------------------


class _FakeConfigCache:
    async def get(self, guild_id: int) -> GuildConfig:
        return GuildConfig(guild_id=guild_id)


def _settings(**kw: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "gateway_join_scan_days": 3,
        "gateway_join_scan_max_channels": 50,
        "gateway_join_scan_messages_per_channel": 200,
        "gateway_max_attachments": 10,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@dataclass
class _Author:
    id: int
    is_bot: bool = False


@dataclass
class _Attachment:
    id: int
    url: str
    filename: str = "a.png"
    media_type: str | None = "image/png"


@dataclass
class _Message:
    id: int
    channel_id: int
    author: _Author
    content: str = ""
    attachments: tuple[_Attachment, ...] = ()
    embeds: tuple[object, ...] = ()
    webhook_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class _FakeHistory:
    """Scripted history: channel_id -> list of messages, or an exception."""

    def __init__(
        self,
        channels: list[int],
        messages: dict[int, list[_Message] | Exception],
        *,
        channels_error: Exception | None = None,
    ) -> None:
        self._channels = channels
        self._messages = messages
        self._channels_error = channels_error
        self.calls: list[tuple[int, int]] = []

    async def list_text_channel_ids(self, guild_id: int) -> list[int]:
        if self._channels_error is not None:
            raise self._channels_error
        return self._channels

    async def fetch_recent_messages(
        self, channel_id: int, *, after: datetime, limit: int
    ) -> list[_Message]:
        self.calls.append((channel_id, limit))
        scripted = self._messages.get(channel_id, [])
        if isinstance(scripted, Exception):
            raise scripted
        return scripted[:limit]


def _service(history: _FakeHistory, bus: _FakeBus, **settings_kw: object) -> GatewayService:
    return GatewayService(
        settings=_settings(**settings_kw),  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        config_cache=_FakeConfigCache(),  # type: ignore[arg-type]
        health=object(),  # type: ignore[arg-type]
        history=history,  # type: ignore[arg-type]
    )


def _image_message(msg_id: int, channel_id: int) -> _Message:
    return _Message(
        id=msg_id,
        channel_id=channel_id,
        author=_Author(id=42),
        attachments=(_Attachment(id=msg_id * 10, url=f"https://cdn.test/{msg_id}.png"),),
    )


async def test_guild_join_backfills_recent_history() -> None:
    bus = _FakeBus()
    history = _FakeHistory(
        channels=[10, 11],
        messages={10: [_image_message(1, 10)], 11: [_image_message(2, 11)]},
    )
    service = _service(history, bus)
    await service.on_guild_join(_JoinEvent(guild_id=123, guild=None))  # type: ignore[arg-type]
    await service.drain()

    image_events = [e for s, e in bus.published if s == SUBJECT_MESSAGE_IMAGE]
    assert {e.message_id for e in image_events} == {1, 2}
    assert all(e.guild_id == 123 for e in image_events)


async def test_backfill_skips_unreadable_channels_and_continues() -> None:
    bus = _FakeBus()
    history = _FakeHistory(
        channels=[10, 11],
        messages={10: RuntimeError("missing permission"), 11: [_image_message(2, 11)]},
    )
    service = _service(history, bus)
    await service.on_guild_join(_JoinEvent(guild_id=123, guild=None))  # type: ignore[arg-type]
    await service.drain()

    image_events = [e for s, e in bus.published if s == SUBJECT_MESSAGE_IMAGE]
    assert [e.message_id for e in image_events] == [2]


async def test_backfill_respects_channel_cap() -> None:
    bus = _FakeBus()
    history = _FakeHistory(
        channels=[10, 11, 12],
        messages={c: [_image_message(c * 100, c)] for c in (10, 11, 12)},
    )
    service = _service(history, bus, gateway_join_scan_max_channels=2)
    await service.on_guild_join(_JoinEvent(guild_id=123, guild=None))  # type: ignore[arg-type]
    await service.drain()

    assert [c for c, _ in history.calls] == [10, 11]


async def test_backfill_disabled_when_days_zero() -> None:
    bus = _FakeBus()
    history = _FakeHistory(channels=[10], messages={10: [_image_message(1, 10)]})
    service = _service(history, bus, gateway_join_scan_days=0)
    await service.on_guild_join(_JoinEvent(guild_id=123, guild=None))  # type: ignore[arg-type]
    await service.drain()

    assert history.calls == []
    assert [s for s, _ in bus.published] == [SUBJECT_GUILD_JOINED]


async def test_backfill_survives_channel_listing_failure() -> None:
    bus = _FakeBus()
    history = _FakeHistory(channels=[], messages={}, channels_error=RuntimeError("no access"))
    service = _service(history, bus)
    await service.on_guild_join(_JoinEvent(guild_id=123, guild=None))  # type: ignore[arg-type]
    await service.drain()

    assert [s for s, _ in bus.published] == [SUBJECT_GUILD_JOINED]


async def test_backfill_does_not_republish_messages_already_seen_live() -> None:
    bus = _FakeBus()
    msg = _image_message(1, 10)
    history = _FakeHistory(channels=[10], messages={10: [msg]})
    service = _service(history, bus)
    # The same message arrived live first (e.g. posted during the join).
    from optimus.services.gateway.bot import message_to_incoming

    await service._scan(message_to_incoming(msg, guild_id=123), trigger="create")  # type: ignore[arg-type]
    live_count = len([s for s, _ in bus.published if s == SUBJECT_MESSAGE_IMAGE])

    await service.on_guild_join(_JoinEvent(guild_id=123, guild=None))  # type: ignore[arg-type]
    await service.drain()
    total = len([s for s, _ in bus.published if s == SUBJECT_MESSAGE_IMAGE])
    assert live_count == total == 1
