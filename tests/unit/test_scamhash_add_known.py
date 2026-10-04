"""``/scamhash add`` says when the server already has an image, instead of re-saving it.

Re-adding the same image never made a second row (its id comes from the
image), but the reply said "Blocked" either way and wrote a fresh audit row,
so it looked like a new save. A re-saved, resized or re-compressed copy got a
new id and *was* stored again, although the scanner already caught it with the
existing entry. Both now answer "already in the list" / "already caught by",
store nothing and audit nothing. Whitelist entries covering the image are
lifted first, and the reply names them.

The ``DbDeps.known_image`` tests run the real matcher on real SQLite; the
handler tests use the in-memory fakes.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.core.config import get_settings
from optimus.core.ratelimit import RateLimit
from optimus.db.models import GuildHash, GuildWhitelist
from optimus.db.repositories import GuildHashRepository, GuildRepository
from optimus.i18n import translate
from optimus.services.interactions.attachment_hash import AttachmentHashes
from optimus.services.interactions.handlers import InteractionContext, handle_command
from optimus.services.interactions.service import DbDeps
from tests.unit.test_interactions_handlers import FakeDeps

GUILD = 111111111111111111
MOD = 444444444444444444
OTHER_MOD = 555555555555555555
MANAGE_GUILD = 1 << 5

BASE = 0x8F3C_21D0_7E44_A519


class _NoopRateLimiter:
    async def acquire(self, key: str, limit: RateLimit, cost: float = 1.0) -> bool:
        return True


def _deps(session: AsyncSession) -> DbDeps:
    return DbDeps(session, _NoopRateLimiter(), get_settings())  # type: ignore[arg-type]


def _flip(value: int, bits: int, seed: int = 7) -> int:
    """``value`` with ``bits`` distinct bits flipped (a slightly different copy)."""
    for bit in random.Random(seed).sample(range(64), bits):
        value ^= 1 << bit
    return value


def _hashes(phash: int, dhash: int, whash: int, ahash: int) -> AttachmentHashes:
    return AttachmentHashes(
        attachment_id=1,
        url="https://cdn.discordapp.com/attachments/1/1/x.png",
        phash=phash,
        dhash=dhash,
        whash=whash,
        ahash=ahash,
        mphash=0,
        mdhash=0,
        mwhash=0,
        mahash=0,
    )


_LISTED = _hashes(BASE, BASE ^ 0xFF00, BASE ^ 0x0FF0, BASE ^ 0x00FF)


async def _list_base(session: AsyncSession) -> GuildHash:
    await GuildRepository(session).get_or_create(GUILD)
    return await GuildHashRepository(session, GUILD).add(
        GuildHash(
            hash_id=f"{BASE:016x}",
            phash=_LISTED.phash,
            dhash=_LISTED.dhash,
            whash=_LISTED.whash,
            ahash=_LISTED.ahash,
            source="review_confirm",
            added_by=OTHER_MOD,
        )
    )


# -- DbDeps.known_image: the real matcher on real SQLite ---------------------


async def test_the_same_image_is_already_listed(session: AsyncSession) -> None:
    row = await _list_base(session)
    known = await _deps(session).known_image(GUILD, _LISTED)
    assert known.entry is not None and known.entry.hash_id == row.hash_id
    assert known.exact


async def test_a_slightly_different_copy_is_already_caught(session: AsyncSession) -> None:
    await _list_base(session)
    copy = _hashes(
        _flip(_LISTED.phash, 3),
        _flip(_LISTED.dhash, 3, seed=8),
        _flip(_LISTED.whash, 2, seed=9),
        _flip(_LISTED.ahash, 2, seed=10),
    )
    assert f"{copy.phash:016x}" != f"{BASE:016x}"  # a different id: would be a second row
    known = await _deps(session).known_image(GUILD, copy)
    assert known.entry is not None and known.entry.hash_id == f"{BASE:016x}"
    assert not known.exact


async def test_a_different_image_is_new(session: AsyncSession) -> None:
    await _list_base(session)
    other = _hashes(~BASE & (2**64 - 1), BASE, ~BASE & (2**64 - 1), BASE)
    known = await _deps(session).known_image(GUILD, other)
    assert known.entry is None


async def test_an_empty_blocklist_knows_nothing(session: AsyncSession) -> None:
    await GuildRepository(session).get_or_create(GUILD)
    assert (await _deps(session).known_image(GUILD, _LISTED)).entry is None


async def test_another_servers_list_does_not_count(session: AsyncSession) -> None:
    await _list_base(session)
    await GuildRepository(session).get_or_create(GUILD + 1)
    assert (await _deps(session).known_image(GUILD + 1, _LISTED)).entry is None


async def test_whitelist_entries_are_listed_and_removed_per_server(session: AsyncSession) -> None:
    await GuildRepository(session).get_or_create(GUILD)
    await GuildRepository(session).get_or_create(GUILD + 1)
    deps = _deps(session)
    mine = await deps.add_whitelist(GUILD, GuildWhitelist(phash=BASE, dhash=0, whash=0))
    theirs = await deps.add_whitelist(GUILD + 1, GuildWhitelist(phash=BASE, dhash=0, whash=0))
    assert [w.id for w in await deps.list_whitelist(GUILD)] == [mine.id]
    # Another server's entry number does nothing here.
    assert await deps.remove_whitelist(GUILD, [int(theirs.id)]) == 0
    assert await deps.remove_whitelist(GUILD, [int(mine.id)]) == 1
    assert await deps.list_whitelist(GUILD) == []
    assert [w.id for w in await deps.list_whitelist(GUILD + 1)] == [theirs.id]


# -- the /scamhash add reply -------------------------------------------------


def _add(images: list[tuple[int, str]], problems: list[str] | None = None) -> InteractionContext:
    return InteractionContext(
        guild_id=GUILD,
        user_id=MOD,
        member_permissions=MANAGE_GUILD,
        command="scamhash",
        subcommand="add",
        options={"images": images, "problems": problems or []},
    )


def _listed(hash_id: str) -> GuildHash:
    return GuildHash(
        hash_id=hash_id,
        phash=int(hash_id, 16),
        dhash=0,
        whash=0,
        ahash=0,
        source="review_confirm",
        added_by=OTHER_MOD,
        created_at=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
    )


def _text(resp: object) -> str:
    return translate(resp.i18n_key, "en", **resp.params)  # type: ignore[attr-defined]


async def test_re_adding_an_image_says_so_and_writes_nothing() -> None:
    deps = FakeDeps()
    deps.hashes[f"{5:016x}"] = _listed(f"{5:016x}")
    resp = await handle_command(_add([(5, "u5")]), deps)
    assert resp.i18n_key == "command.add_known"
    stamp = int(datetime(2026, 10, 3, 12, 0, tzinfo=UTC).timestamp())
    assert _text(resp) == (
        f"Already in the list as `{5:016x}` — Confirm scam by <@{OTHER_MOD}> <t:{stamp}:d>."
    )
    assert deps.audits == []


async def test_a_copy_the_scanner_catches_is_not_added_again() -> None:
    deps = FakeDeps(near_copies={f"{6:016x}": f"{5:016x}"})
    deps.hashes[f"{5:016x}"] = _listed(f"{5:016x}")
    resp = await handle_command(_add([(6, "u6")]), deps)
    assert resp.i18n_key == "command.add_known"
    assert _text(resp).startswith(f"Already caught by `{5:016x}` — Confirm scam by")
    assert _text(resp).endswith("Not added again.")
    assert set(deps.hashes) == {f"{5:016x}"}
    assert deps.audits == []


async def test_new_and_known_images_are_reported_together() -> None:
    deps = FakeDeps(near_copies={f"{7:016x}": f"{5:016x}"})
    deps.hashes[f"{5:016x}"] = _listed(f"{5:016x}")
    resp = await handle_command(_add([(5, "u5"), (6, "u6"), (7, "u7")]), deps)
    assert resp.i18n_key == "command.hashes_added"
    lines = _text(resp).splitlines()
    assert lines[0] == f"Blocked 1 image(s): `{6:016x}`. Future posts of them will be caught."
    assert lines[1].startswith(f"Already in the list as `{5:016x}`")
    assert lines[2].startswith(f"Already caught by `{5:016x}`")
    assert [a[3] for a in deps.audits] == [f"{6:016x}"]


async def test_several_known_images_and_nothing_new() -> None:
    deps = FakeDeps()
    for i in (5, 6):
        deps.hashes[f"{i:016x}"] = _listed(f"{i:016x}")
    resp = await handle_command(_add([(5, "u5"), (6, "u6")]), deps)
    assert resp.i18n_key == "command.add_nothing_blocked"
    lines = _text(resp).splitlines()
    assert lines[0] == "Nothing was blocked:"
    assert len(lines) == 3


async def test_a_known_image_next_to_a_skipped_input_lists_both() -> None:
    deps = FakeDeps()
    deps.hashes[f"{5:016x}"] = _listed(f"{5:016x}")
    resp = await handle_command(_add([(5, "u5")], problems=["bad_url"]), deps)
    assert resp.i18n_key == "command.add_nothing_blocked"
    lines = _text(resp).splitlines()
    assert len(lines) == 3
    assert lines[1].startswith(f"Already in the list as `{5:016x}`")
    assert lines[2] == "Skipped `url:` — not a Discord image link."


async def test_the_same_new_image_twice_in_one_command_is_one_add_and_no_note() -> None:
    deps = FakeDeps(attachment_outcomes={6: f"{5:016x}"})
    resp = await handle_command(_add([(5, "u5"), (6, "u6")]), deps)
    assert resp.i18n_key == "command.hashes_added"
    assert _text(resp) == (f"Blocked 1 image(s): `{5:016x}`. Future posts of them will be caught.")
    assert len(deps.audits) == 1


async def test_adding_a_whitelisted_image_lifts_the_whitelist() -> None:
    deps = FakeDeps()
    await deps.add_whitelist(GUILD, GuildWhitelist(phash=5, dhash=0, whash=0))
    resp = await handle_command(_add([(5, "u5")]), deps)
    assert resp.i18n_key == "command.hashes_added"
    assert f"{5:016x}" in deps.hashes
    assert deps.whitelisted == []
    assert _text(resp).splitlines()[1] == (
        "Removed from the whitelist: #1. Optimus flags these images again."
    )
    assert ("scamhash.unwhitelist", "#1 (scamhash add)") in [(a[2], a[3]) for a in deps.audits]


async def test_an_already_listed_whitelisted_image_still_lifts_the_whitelist() -> None:
    # The case that started this: a real scam image both listed and whitelisted.
    deps = FakeDeps()
    await handle_command(_add([(5, "u5")]), deps)
    await deps.add_whitelist(GUILD, GuildWhitelist(phash=5, dhash=0, whash=0))
    resp = await handle_command(_add([(5, "u5")]), deps)
    assert resp.i18n_key == "command.add_nothing_blocked"
    assert deps.whitelisted == []
    assert "Removed from the whitelist: #1." in _text(resp)


async def test_a_far_whitelist_entry_is_left_alone() -> None:
    deps = FakeDeps()
    await deps.add_whitelist(GUILD, GuildWhitelist(phash=~5 & (2**64 - 1), dhash=0, whash=0))
    await handle_command(_add([(5, "u5")]), deps)
    assert len(deps.whitelisted) == 1


async def test_a_new_clean_image_keeps_the_short_reply() -> None:
    deps = FakeDeps()
    resp = await handle_command(_add([(5, "u5")]), deps)
    assert resp.i18n_key == "command.hash_added"


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_the_new_lines_exist_in_every_language(locale: str) -> None:
    for key in ("add_already_listed", "add_already_caught", "add_known", "add_unwhitelisted"):
        assert translate(
            f"command.{key}", locale, hash_id="x", origin="y", notes="z", entries="#1"
        ) != (f"command.{key}")


async def test_a_campaign_cleanup_entry_is_credited_to_optimus_not_a_broken_mention() -> None:
    # The campaign cleanup stores the bot's own actor id (0); "<@0>" rendered
    # as a broken mention in the add reply and in /scamhash list.
    deps = FakeDeps()
    row = _listed(f"{5:016x}")
    row.source = "campaign_sweep"
    row.added_by = 0
    deps.hashes[row.hash_id] = row
    resp = await handle_command(_add([(5, "u5")]), deps)
    stamp = int(datetime(2026, 10, 3, 12, 0, tzinfo=UTC).timestamp())
    assert _text(resp) == (
        f"Already in the list as `{5:016x}` — campaign cleanup by Optimus <t:{stamp}:d>."
    )
    listing = await handle_command(
        InteractionContext(
            guild_id=GUILD,
            user_id=MOD,
            member_permissions=MANAGE_GUILD,
            command="scamhash",
            subcommand="list",
        ),
        deps,
    )
    assert "by Optimus" in _text(listing)
    assert "<@0>" not in _text(listing)
