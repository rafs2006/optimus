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
from optimus.services.interactions.logic import (
    AddProblem,
    CommandError,
    InteractionRejected,
    message_link_guild,
)
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
    assert ctx.options == {
        "images": [(1, "https://cdn/1.png"), (3, "https://cdn/3.png")],
        "problems": [],
    }


async def test_a_message_without_images_is_a_problem() -> None:
    ctx = await _resolve_add_options(
        _ctx(message=LINK), _NO_UPLOAD, rest=_Rest([_att(2, "text/plain")])
    )
    assert ctx.options == {"images": [], "problems": ["message_no_images"]}


@pytest.mark.parametrize(
    ("error", "problem"),
    [
        (RuntimeError("Missing Access"), "message_unreadable"),
        (hikari.NotFoundError("u", {}, b"", "not found"), "message_not_found"),
    ],
)
async def test_a_bad_message_is_a_problem_not_a_rejection(error: Exception, problem: str) -> None:
    rest = _Rest([], error=error)
    ctx = await _resolve_add_options(_ctx(message=LINK), _NO_UPLOAD, rest=rest)
    assert ctx.options == {"images": [], "problems": [problem]}


async def test_an_invalid_message_reference_is_not_found() -> None:
    ctx = await _resolve_add_options(_ctx(message="not a link"), _NO_UPLOAD, rest=_Rest([]))
    assert ctx.options["problems"] == ["message_not_found"]


async def test_a_message_link_needs_rest() -> None:
    ctx = await _resolve_add_options(_ctx(message=LINK), _NO_UPLOAD)
    assert ctx.options["problems"] == ["message_unreadable"]


async def test_a_link_to_another_server_is_refused_without_fetching() -> None:
    rest = _Rest([_att(1)])
    other = "https://discord.com/channels/999/500/600"
    ctx = await _resolve_add_options(_ctx(message=other), _NO_UPLOAD, rest=rest)
    assert rest.calls == []
    assert ctx.options == {"images": [], "problems": ["message_other_server"]}
    # A bare id stays in the invoking channel, so it is always this server.
    bare = await _resolve_add_options(_ctx(message="600"), _NO_UPLOAD, rest=rest)
    assert bare.options["problems"] == []
    assert rest.calls == [(9, 600)]


async def test_an_image_link_and_a_bad_one() -> None:
    ok = await _resolve_add_options(_ctx(url=CDN), _NO_UPLOAD)
    assert ok.options == {"images": [(222, CDN)], "problems": []}
    bad = await _resolve_add_options(_ctx(url="https://example.com/a.png"), _NO_UPLOAD)
    assert bad.options == {"images": [], "problems": ["bad_url"]}


# Concern 1 and 3: one bad input keeps the good ones.


async def test_a_bad_url_keeps_the_upload_and_the_message() -> None:
    upload = SimpleNamespace(resolved=SimpleNamespace(attachments={1: _att(1)}), channel_id=9)
    ctx = await _resolve_add_options(
        _ctx(image=1, message=LINK, url="https://example.com/x.png"),
        upload,
        rest=_Rest([_att(2)]),
    )
    assert [a for a, _u in ctx.options["images"]] == [1, 2]
    assert ctx.options["problems"] == ["bad_url"]


async def test_an_unreadable_message_keeps_the_upload_and_the_url() -> None:
    upload = SimpleNamespace(resolved=SimpleNamespace(attachments={1: _att(1)}), channel_id=9)
    ctx = await _resolve_add_options(
        _ctx(image=1, message=LINK, url=CDN),
        upload,
        rest=_Rest([], error=RuntimeError("Missing Access")),
    )
    assert [a for a, _u in ctx.options["images"]] == [1, 222]
    assert ctx.options["problems"] == ["message_unreadable"]


# Concern 2: a non-image upload next to a valid message is reported.


async def test_a_pdf_upload_next_to_a_message_is_reported() -> None:
    upload = SimpleNamespace(
        resolved=SimpleNamespace(attachments={1: _att(1, "application/pdf")}), channel_id=9
    )
    ctx = await _resolve_add_options(
        _ctx(image=1, message=LINK), upload, rest=_Rest([_att(2), _att(3)])
    )
    assert [a for a, _u in ctx.options["images"]] == [2, 3]
    assert ctx.options["problems"] == ["not_image"]


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
    assert set(deps.hashes) == {f"{i:016x}" for i in (5, 6, 7)}
    assert [a[2] for a in deps.audits] == ["scamhash.add"] * 3
    text = translate(resp.i18n_key, "en", **resp.params)
    # Concern 4: no "failed" tail when nothing failed.
    assert text == (
        f"Blocked 3 image(s): `{5:016x}`, `{6:016x}`, `{7:016x}`. "
        "Future posts of them will be caught."
    )


async def test_a_failed_image_does_not_block_the_rest() -> None:
    deps = FakeDeps(attachment_outcomes={6: AttachmentHashError("expired link")})
    resp = await handle_command(_add(images=[(5, "u5"), (6, "u6")]), deps)
    assert resp.i18n_key == "command.hashes_added"
    assert resp.params["count"] == 1
    assert set(deps.hashes) == {f"{5:016x}"}
    assert translate(resp.i18n_key, "en", **resp.params).endswith("\nCouldn't fetch 1 image(s).")


async def test_skipped_inputs_are_listed_under_what_was_blocked() -> None:
    deps = FakeDeps()
    resp = await handle_command(
        _add(images=[(5, "u5"), (6, "u6")], problems=["bad_url", "not_image"]), deps
    )
    assert resp.i18n_key == "command.hashes_added"
    text = translate(resp.i18n_key, "en", **resp.params)
    assert text.splitlines()[1:] == [
        "Skipped `url:` — not a Discord image link.",
        "Skipped `image:` — that file isn't an image.",
    ]
    assert set(deps.hashes) == {f"{5:016x}", f"{6:016x}"}


async def test_one_image_with_a_skipped_input_says_both() -> None:
    deps = FakeDeps()
    resp = await handle_command(_add(images=[(5, "u5")], problems=["bad_url"]), deps)
    assert resp.i18n_key == "command.hashes_added"
    assert resp.params["count"] == 1


async def test_one_clean_image_keeps_the_short_reply() -> None:
    deps = FakeDeps()
    resp = await handle_command(_add(images=[(5, "u5")], problems=[]), deps)
    assert resp.i18n_key == "command.hash_added"


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
    ("problem", "key"),
    [
        ("bad_url", "command.add_bad_url"),
        ("message_no_images", "command.add_message_no_images"),
        ("not_image", "command.add_not_image"),
        ("message_other_server", "command.add_message_other_server"),
    ],
)
async def test_a_single_problem_keeps_its_specific_answer(problem: str, key: str) -> None:
    deps = FakeDeps()
    resp = await handle_command(_add(images=[], problems=[problem]), deps)
    assert resp.i18n_key == key
    assert not deps.hashes


@pytest.mark.parametrize(
    ("problem", "error"),
    [
        ("message_not_found", CommandError.MESSAGE_NOT_FOUND),
        ("message_unreadable", CommandError.FETCH_FAILED),
    ],
)
async def test_a_single_message_problem_keeps_the_existing_error(
    problem: str, error: CommandError
) -> None:
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_add(images=[], problems=[problem]), FakeDeps())
    assert exc.value.reason is error


async def test_no_input_at_all_asks_for_one() -> None:
    resp = await handle_command(_add(), FakeDeps())
    assert resp.i18n_key == "command.add_not_image"


async def test_several_problems_and_nothing_blocked_lists_them_all() -> None:
    deps = FakeDeps(attachment_outcomes={5: AttachmentHashError("expired")})
    resp = await handle_command(
        _add(images=[(5, "u5")], problems=["bad_url", "message_other_server"]), deps
    )
    assert resp.i18n_key == "command.add_nothing_blocked"
    assert translate(resp.i18n_key, "en", **resp.params).splitlines() == [
        "Nothing was blocked:",
        "Skipped `url:` — not a Discord image link.",
        "Skipped `message:` — that message is on another server.",
        "Couldn't fetch 1 image(s).",
    ]


def test_every_problem_has_a_skip_line_in_every_language() -> None:
    for problem in AddProblem:
        for locale in ("en", "sr"):
            key = f"command.add_skip_{problem.value}"
            assert translate(key, locale) != key


def test_message_link_guild() -> None:
    assert message_link_guild("https://discord.com/channels/5/6/7") == 5
    assert message_link_guild("https://ptb.discord.com/channels/5/6/7 ") == 5
    assert message_link_guild("7") is None
