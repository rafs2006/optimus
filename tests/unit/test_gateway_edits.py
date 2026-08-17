"""Unit tests for edited-message scanning (message update events).

The bypass being closed: post an innocent message, then *edit* the scam image
in. Discord also delivers link-unfurl embeds as partial updates, so the update
path must tolerate ``UNDEFINED`` fields and fall back to a REST fetch when the
author is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import hikari

import optimus.services.gateway.bot as gateway_bot
from optimus.contracts.events import SUBJECT_MESSAGE_IMAGE
from optimus.core.guild_config import GuildConfig
from optimus.services.gateway.bot import (
    GatewayService,
    message_to_incoming,
    to_incoming_update,
)


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, subject: str, event: Any) -> None:
        self.published.append((subject, event))


class _FakeConfigCache:
    def __init__(self, config: GuildConfig) -> None:
        self._config = config

    async def get(self, guild_id: int) -> GuildConfig:
        return self._config


@dataclass
class _PartialMessage:
    id: int = 3
    author: Any = hikari.UNDEFINED
    attachments: Any = hikari.UNDEFINED
    embeds: Any = hikari.UNDEFINED
    content: Any = hikari.UNDEFINED
    member: Any = hikari.UNDEFINED
    webhook_id: Any = hikari.UNDEFINED


@dataclass
class _UpdateEvent:
    message: _PartialMessage
    guild_id: int = 1
    channel_id: int = 2


def _author(user_id: int = 4, *, is_bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, is_bot=is_bot)


def _attachment(url: str = "https://cdn.test/a.png", att_id: int = 10) -> SimpleNamespace:
    return SimpleNamespace(id=att_id, url=url, filename="a.png", media_type="image/png")


def _embed(url: str = "https://cdn.test/e.png") -> SimpleNamespace:
    return SimpleNamespace(image=SimpleNamespace(url=url), thumbnail=None)


def _service(
    fetch: Any = None, config: GuildConfig | None = None
) -> tuple[GatewayService, _FakeBus]:
    bus = _FakeBus()
    service = GatewayService(
        settings=SimpleNamespace(gateway_max_attachments=10),  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        config_cache=_FakeConfigCache(config or GuildConfig(guild_id=1)),  # type: ignore[arg-type]
        health=object(),  # type: ignore[arg-type]
        fetch_message=fetch,
    )
    return service, bus


async def test_update_with_new_attachment_publishes() -> None:
    service, bus = _service()
    event = _UpdateEvent(message=_PartialMessage(author=_author(), attachments=[_attachment()]))

    await service.on_message_update(event)  # type: ignore[arg-type]

    assert len(bus.published) == 1
    subject, published = bus.published[0]
    assert subject == SUBJECT_MESSAGE_IMAGE
    assert published.url == "https://cdn.test/a.png"
    assert published.uploader_id == 4


async def test_repeated_edits_publish_each_image_once() -> None:
    service, bus = _service()
    event = _UpdateEvent(message=_PartialMessage(author=_author(), attachments=[_attachment()]))

    await service.on_message_update(event)  # type: ignore[arg-type]
    await service.on_message_update(event)  # type: ignore[arg-type]

    assert len(bus.published) == 1


async def test_edit_after_create_only_publishes_new_image() -> None:
    service, bus = _service()
    from optimus.services.gateway.extract import Attachment, IncomingMessage

    original = IncomingMessage(
        guild_id=1,
        channel_id=2,
        message_id=3,
        author_id=4,
        attachments=(
            Attachment(
                id=10, url="https://cdn.test/a.png", filename="a.png", content_type="image/png"
            ),
        ),
    )
    await service._scan(original, trigger="create")
    assert len(bus.published) == 1

    edited = _UpdateEvent(
        message=_PartialMessage(
            author=_author(),
            attachments=[_attachment(), _attachment(url="https://cdn.test/new.png", att_id=11)],
        )
    )
    await service.on_message_update(edited)  # type: ignore[arg-type]

    assert len(bus.published) == 2
    assert bus.published[1][1].url == "https://cdn.test/new.png"


async def test_unfurl_partial_falls_back_to_rest_fetch() -> None:
    calls: list[tuple[int, int]] = []
    full = SimpleNamespace(
        id=3,
        channel_id=2,
        author=_author(),
        content=None,
        attachments=[],
        embeds=[_embed()],
        webhook_id=None,
    )

    async def fetch(channel_id: int, message_id: int) -> Any:
        calls.append((channel_id, message_id))
        return full

    service, bus = _service(fetch=fetch)
    event = _UpdateEvent(message=_PartialMessage(embeds=[_embed()]))

    await service.on_message_update(event)  # type: ignore[arg-type]

    assert calls == [(2, 3)]
    assert len(bus.published) == 1
    assert bus.published[0][1].url == "https://cdn.test/e.png"


async def test_partial_without_images_skips_rest_fetch() -> None:
    calls: list[tuple[int, int]] = []

    async def fetch(channel_id: int, message_id: int) -> Any:
        calls.append((channel_id, message_id))
        raise AssertionError("must not fetch")

    service, bus = _service(fetch=fetch)
    event = _UpdateEvent(message=_PartialMessage(content="edited text, no images"))

    await service.on_message_update(event)  # type: ignore[arg-type]

    assert calls == []
    assert bus.published == []


async def test_fetch_failure_is_swallowed() -> None:
    async def fetch(channel_id: int, message_id: int) -> Any:
        raise RuntimeError("discord 500")

    service, bus = _service(fetch=fetch)
    event = _UpdateEvent(message=_PartialMessage(embeds=[_embed()]))

    await service.on_message_update(event)  # type: ignore[arg-type]

    assert bus.published == []


async def test_update_from_bot_author_is_filtered() -> None:
    service, bus = _service()
    event = _UpdateEvent(
        message=_PartialMessage(author=_author(is_bot=True), attachments=[_attachment()])
    )

    await service.on_message_update(event)  # type: ignore[arg-type]

    assert bus.published == []


def test_to_incoming_update_treats_undefined_as_empty() -> None:
    event = _UpdateEvent(message=_PartialMessage(author=_author(), content="just text"))
    msg = to_incoming_update(event)  # type: ignore[arg-type]

    assert msg is not None
    assert msg.attachments == ()
    assert msg.embed_image_urls == ()
    assert msg.is_webhook is False
    assert msg.author_role_ids == frozenset()


def test_to_incoming_update_returns_none_without_author() -> None:
    event = _UpdateEvent(message=_PartialMessage(embeds=[_embed()]))
    assert to_incoming_update(event) is None  # type: ignore[arg-type]


def test_message_to_incoming_adapts_rest_message() -> None:
    full = SimpleNamespace(
        id=3,
        channel_id=2,
        author=_author(),
        content="see https://x.test/c.png",
        attachments=[_attachment()],
        embeds=[_embed()],
        webhook_id=None,
    )
    msg = message_to_incoming(full, guild_id=1)  # type: ignore[arg-type]

    assert msg.guild_id == 1
    assert msg.author_id == 4
    assert [a.url for a in msg.attachments] == ["https://cdn.test/a.png"]
    assert msg.embed_image_urls == ("https://cdn.test/e.png",)
    assert msg.content == "see https://x.test/c.png"
    assert msg.is_webhook is False


async def test_seen_cache_is_bounded(monkeypatch: Any) -> None:
    monkeypatch.setattr(gateway_bot, "_SEEN_CACHE_MAX", 4)
    service, _ = _service()
    for i in range(10):
        service._mark_seen(3, f"https://cdn.test/{i}.png")
    assert len(service._seen) == 4
