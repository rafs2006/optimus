"""``/scamhash add`` takes an upload, a message link, or a Discord image link.

Blocking an image used to mean downloading it and uploading it again. Now a
moderator can point at the message (every image on it is blocked) or paste
one image's link (for one specific image of a multi-image post). Nothing is
acted on -- that is ``/scamhash review`` -- the images are only blocklisted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import hikari
import pytest

from optimus.i18n import translate
from optimus.services.interactions.attachment_hash import AttachmentHashError
from optimus.services.interactions.handlers import InteractionContext, handle_command
from optimus.services.interactions.logic import CommandError, InteractionRejected
from optimus.services.interactions.service import (
    MAX_ADD_IMAGES,
    _resolve_add_options,
    parse_cdn_image_url,
)
from tests.unit.test_interactions_handlers import MANAGE, FakeDeps

CDN = "https://cdn.discordapp.com/attachments/111/222/scam.png?ex=1&is=2&hm=3"

# --- image links ---------------------------------------------------------------


def test_a_discord_image_link_yields_its_attachment_id() -> None:
    assert parse_cdn_image_url(CDN) == (222, CDN)
    media = "https://media.discordapp.net/attachments/1/2/a.webp?width=400"
    assert parse_cdn_image_url(f"  {media} ") == (2, media)


@pytest.mark.parametrize(
    "raw",
    [
        "http://cdn.discordapp.com/attachments/1/2/a.png",  # not https
        "https://example.com/attachments/1/2/a.png",  # not Discord
        "https://cdn.discordapp.com.evil.test/attachments/1/2/a.png",
        "https://cdn.discordapp.com/avatars/1/2.png",  # not an attachment
        "https://cdn.discordapp.com/attachments/1/x/a.png",  # no id
        "https://discord.com/channels/1/2/3",  # a message link, not an image
        "not a url",
        "",
    ],
)
def test_anything_else_is_refused(raw: str) -> None:
    assert parse_cdn_image_url(raw) is None


# --- resolving the options -------------------------------------------------------


def _ctx(**options: Any) -> InteractionContext:
    return InteractionContext(
        guild_id=123,
        user_id=456,
        member_permissions=MANAGE,
        command="scamhash",
        subcommand="add",
        options=options,
    )


def _att(att_id: int, media_type: str = "image/png") -> SimpleNamespace:
    return SimpleNamespace(id=att_id, url=f"https://cdn/{att_id}.png", media_type=media_type)


class _Rest:
    def __init__(self, attachments: list[Any], error: Exception | None = None) -> None:
        self._attachments = attachments
        self._error = error
        self.calls: list[tuple[int, int]] = []

    async def fetch_message(self, channel_id: int, message_id: int) -> Any:
        self.calls.append((channel_id, message_id))
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            channel_id=channel_id,
            id=message_id,
            author=SimpleNamespace(id=777),
            attachments=self._attachments,
        )


_NO_UPLOAD = SimpleNamespace(resolved=None, channel_id=9)
LINK = "https://discord.com/channels/123/500/600"


async def test_a_message_link_blocks_every_image_on_it() -> None:
    rest = _Rest([_att(1), _att(2, "application/pdf"), _att(3)])
    ctx = await _resolve_add_options(_ctx(message=LINK), _NO_UPLOAD, rest=rest)
    assert rest.calls == [(500, 600)]
    assert ctx.options == {"images": [(1, "https://cdn/1.png"), (3, "https://cdn/3.png")]}


async def test_a_message_without_images_is_flagged() -> None:
    ctx = await _resolve_add_options(
        _ctx(message=LINK), _NO_UPLOAD, rest=_Rest([_att(2, "text/plain")])
    )
    assert ctx.options == {"images": [], "message_no_images": True}


async def test_an_unreadable_message_is_a_clear_rejection() -> None:
    rest = _Rest([], error=RuntimeError("Missing Access"))
    with pytest.raises(InteractionRejected) as exc:
        await _resolve_add_options(_ctx(message=LINK), _NO_UPLOAD, rest=rest)
    assert exc.value.reason is CommandError.FETCH_FAILED


async def test_a_deleted_message_says_so() -> None:
    gone = hikari.NotFoundError("u", {}, b"", "not found")
    with pytest.raises(InteractionRejected) as exc:
        await _resolve_add_options(_ctx(message=LINK), _NO_UPLOAD, rest=_Rest([], error=gone))
    assert exc.value.reason is CommandError.MESSAGE_NOT_FOUND


async def test_a_message_link_needs_rest() -> None:
    with pytest.raises(InteractionRejected):
        await _resolve_add_options(_ctx(message=LINK), _NO_UPLOAD)


async def test_an_image_link_and_a_bad_one() -> None:
    ok = await _resolve_add_options(_ctx(url=CDN), _NO_UPLOAD)
    assert ok.options == {"images": [(222, CDN)]}
    bad = await _resolve_add_options(_ctx(url="https://example.com/a.png"), _NO_UPLOAD)
    assert bad.options == {"images": [], "bad_url": True}


async def test_sources_combine_without_duplicates_and_are_capped() -> None:
    upload = SimpleNamespace(
        resolved=SimpleNamespace(attachments={1: _att(1)}),
        channel_id=9,
    )
    many = [_att(i) for i in range(1, MAX_ADD_IMAGES + 5)]
    ctx = await _resolve_add_options(_ctx(image=1, message=LINK, url=CDN), upload, rest=_Rest(many))
    ids = [a for a, _u in ctx.options["images"]]
    assert ids[0] == 1 and ids.count(1) == 1  # the upload, not repeated
    assert len(ids) == MAX_ADD_IMAGES


# --- the handler ---------------------------------------------------------------


def _add(**options: Any) -> InteractionContext:
    return InteractionContext(
        guild_id=1,
        user_id=99,
        member_permissions=MANAGE,
        command="scamhash",
        subcommand="add",
        options=options,
    )


async def test_several_images_are_blocked_and_reported_together() -> None:
    deps = FakeDeps()
    resp = await handle_command(_add(images=[(5, "u5"), (6, "u6"), (7, "u7")]), deps)
    assert resp.i18n_key == "command.hashes_added"
    assert resp.params["count"] == 3
    assert resp.params["failed"] == 0
    assert set(deps.hashes) == {f"{i:016x}" for i in (5, 6, 7)}
    assert [a[2] for a in deps.audits] == ["scamhash.add"] * 3
    text = translate(resp.i18n_key, "en", **resp.params)
    assert f"`{5:016x}`" in text and "Failed to fetch: 0" in text


async def test_a_failed_image_does_not_block_the_rest() -> None:
    deps = FakeDeps(attachment_outcomes={6: AttachmentHashError("expired link")})
    resp = await handle_command(_add(images=[(5, "u5"), (6, "u6")]), deps)
    assert resp.i18n_key == "command.hashes_added"
    assert resp.params["count"] == 1
    assert resp.params["failed"] == 1
    assert set(deps.hashes) == {f"{5:016x}"}


async def test_when_every_image_fails_the_reason_is_shown() -> None:
    deps = FakeDeps(
        attachment_outcomes={5: AttachmentHashError("expired"), 6: AttachmentHashError("bad")}
    )
    resp = await handle_command(_add(images=[(5, "u5"), (6, "u6")]), deps)
    assert resp.i18n_key == "command.add_fetch_failed"
    assert resp.params["reason"] == "expired"
    assert not deps.hashes


async def test_the_same_image_twice_is_one_entry_and_one_audit() -> None:
    deps = FakeDeps(attachment_outcomes={6: f"{5:016x}"})
    resp = await handle_command(_add(images=[(5, "u5"), (6, "u6")]), deps)
    assert resp.params["count"] == 1
    assert len(deps.audits) == 1


@pytest.mark.parametrize(
    ("options", "key"),
    [
        ({"images": [], "bad_url": True}, "command.add_bad_url"),
        ({"images": [], "message_no_images": True}, "command.add_message_no_images"),
        ({"images": [], "not_image": True}, "command.add_not_image"),
        ({}, "command.add_not_image"),
    ],
)
async def test_nothing_usable_gets_a_precise_answer(options: dict[str, Any], key: str) -> None:
    deps = FakeDeps()
    resp = await handle_command(_add(**options), deps)
    assert resp.i18n_key == key
    assert not deps.hashes
