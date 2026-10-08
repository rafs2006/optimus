"""Linked servers keep one blocklist.

Two servers run by the same people had 66 and 20-odd entries, and the only way
to share them was export and import. ``/global link_server`` puts servers in
one group: linking copies each server's entries to the others, and from then
on every addition or removal on one server reaches all of them -- ``/scamhash
add``, Confirm scam, the campaign sweep's harvest, ``/scamhash remove``. A
copy is a local entry on each server, so it is acted on like the server's own.

Real ``DbDeps`` and repositories against SQLite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.db.engine import create_engine, create_session_factory
from optimus.db.models import Base, Detection, Guild
from optimus.db.repositories import (
    LINKED_SOURCE,
    GuildHashRepository,
    GuildLinkRepository,
    GuildRepository,
    copy_hash_to_peers,
    remove_hash_from_peers,
    sync_group,
)
from optimus.services.moderation.sweep import CampaignSweeper
from tests.unit.test_guild_hash_per_server import _deps, _gh

A, B, C = 111, 222, 333


async def _ids(session: AsyncSession, guild_id: int) -> set[str]:
    return {r.hash_id for r in await GuildHashRepository(session, guild_id).list_active()}


async def _seed(session: AsyncSession, guild_id: int, *phashes: int) -> None:
    await GuildRepository(session).get_or_create(guild_id)
    for p in phashes:
        await GuildHashRepository(session, guild_id).add(_gh(p, added_by=5))


# --- Groups -----------------------------------------------------------------


async def test_link_unlink_and_merge(session: AsyncSession) -> None:
    repo = GuildLinkRepository(session)
    g1 = await repo.link(A, B, added_by=9)
    assert await repo.peers(A) == [B]
    assert await repo.link(B, C, added_by=9) == g1  # C joins the existing group
    assert await repo.peers(A) == [B, C]

    other = await repo.link(444, 555, added_by=9)
    assert other != g1
    merged = await repo.link(A, 444, added_by=9)  # two groups become one
    assert await repo.members(merged) == [A, B, C, 444, 555]

    assert await repo.unlink(C)
    assert not await repo.unlink(C)
    assert C not in await repo.members(merged)


async def test_a_group_left_with_one_server_dissolves(session: AsyncSession) -> None:
    repo = GuildLinkRepository(session)
    await repo.link(A, B, added_by=9)
    await repo.unlink(A)
    assert await repo.group_of(B) is None
    assert await repo.groups() == {}


# --- Copying ----------------------------------------------------------------


async def test_linking_gives_every_server_the_union(session: AsyncSession) -> None:
    await _seed(session, A, 1, 2, 3)
    await _seed(session, B, 3, 4)
    group = await GuildLinkRepository(session).link(A, B, added_by=9)
    gained = await sync_group(session, group)

    assert gained == {A: 1, B: 2}
    assert await _ids(session, A) == await _ids(session, B)
    copied = await GuildHashRepository(session, B).get(f"{1:016x}")
    assert copied is not None
    assert copied.source == LINKED_SOURCE
    assert copied.added_by == 5  # attribution travels with the entry
    own = await GuildHashRepository(session, B).get(f"{3:016x}")
    assert own is not None and own.source == "local"  # an own entry is never replaced


async def test_copy_and_remove_reach_peers_only(session: AsyncSession) -> None:
    await _seed(session, A)
    await _seed(session, 999)  # not linked
    await GuildLinkRepository(session).link(A, B, added_by=9)
    gh = await GuildHashRepository(session, A).add(_gh(7))

    assert await copy_hash_to_peers(session, A, gh) == [B]
    assert await copy_hash_to_peers(session, A, gh) == []  # already there
    assert await _ids(session, 999) == set()

    assert await remove_hash_from_peers(session, A, gh.hash_id) == [B]
    assert await _ids(session, B) == set()


async def test_removal_never_takes_a_peers_own_entry(session: AsyncSession) -> None:
    # B listed the image itself before the link: A's moderators removing it
    # on A must not take it off B's list. Copies (source "linked") do go.
    await _seed(session, A, 7)
    await _seed(session, B, 7)
    await _seed(session, C)
    group = await GuildLinkRepository(session).link(A, B, added_by=9)
    await GuildLinkRepository(session).link(A, C, added_by=9)
    await sync_group(session, group)  # C gets a copy

    assert await remove_hash_from_peers(session, A, f"{7:016x}") == [C]
    assert await _ids(session, B) == {f"{7:016x}"}
    assert await _ids(session, C) == set()


async def test_an_unlinked_server_copies_nothing(session: AsyncSession) -> None:
    await _seed(session, A)
    gh = await GuildHashRepository(session, A).add(_gh(7))
    assert await copy_hash_to_peers(session, A, gh) == []


# --- Through the real interaction deps --------------------------------------


async def test_link_guilds_syncs_and_reloads_indexes(session: AsyncSession) -> None:
    await _seed(session, A, 1, 2)
    await _seed(session, B)
    deps = _deps(session)
    result = await deps.link_guilds(A, B, added_by=9)

    assert result.members == (A, B)
    assert result.gained == {A: 0, B: 2}
    assert deps.pending_index_invalidations == {B}
    assert await deps.list_links() == [[A, B]]


async def test_an_added_entry_reaches_linked_servers(session: AsyncSession) -> None:
    await _seed(session, A)
    await _seed(session, B)
    deps = _deps(session)
    await deps.link_guilds(A, B, added_by=9)
    deps.pending_index_invalidations.clear()

    await deps.add_guild_hash(A, _gh(42))
    assert f"{42:016x}" in await _ids(session, B)
    assert deps.pending_index_invalidations == {A, B}


async def test_a_removed_entry_goes_from_linked_servers(session: AsyncSession) -> None:
    await _seed(session, A, 42)
    await _seed(session, B)
    deps = _deps(session)
    await deps.link_guilds(A, B, added_by=9)
    deps.pending_index_invalidations.clear()

    # Removed where it was added: the copy on B goes too.
    assert await deps.remove_guild_hash(A, f"{42:016x}") == 1
    assert await _ids(session, B) == set()
    assert deps.pending_index_invalidations == {A, B}


async def test_removing_a_copy_keeps_the_owners_entry(session: AsyncSession) -> None:
    await _seed(session, A, 42)
    await _seed(session, B)
    deps = _deps(session)
    await deps.link_guilds(A, B, added_by=9)
    deps.pending_index_invalidations.clear()

    # B drops its copy; A's own entry is A's decision.
    assert await deps.remove_guild_hash(B, f"{42:016x}") == 1
    assert await _ids(session, B) == set()
    assert await _ids(session, A) == {f"{42:016x}"}
    assert deps.pending_index_invalidations == {B}


async def test_unlink_keeps_the_copied_entries(session: AsyncSession) -> None:
    await _seed(session, A, 1)
    await _seed(session, B)
    deps = _deps(session)
    await deps.link_guilds(A, B, added_by=9)
    assert await deps.unlink_guild(B)
    assert await _ids(session, B) == {f"{1:016x}"}
    await deps.add_guild_hash(A, _gh(2))
    assert f"{2:016x}" not in await _ids(session, B)  # no longer linked


async def test_delete_server_data_unlinks_first(session: AsyncSession) -> None:
    """/delete_server_data must leave the group, or copies would refill it."""
    from optimus.db.repositories import GuildPurgeRepository

    await _seed(session, A, 1)
    await _seed(session, B)
    deps = _deps(session)
    await deps.link_guilds(A, B, added_by=9)
    await GuildPurgeRepository(session, B).purge()

    assert await GuildLinkRepository(session).group_of(B) is None
    assert await GuildLinkRepository(session).group_of(A) is None  # a group of one dissolves
    await _deps(session).add_guild_hash(A, _gh(2))
    assert await GuildRepository(session).get(B) is None  # nothing recreated
    assert await _ids(session, B) == set()


async def test_an_import_looks_the_link_up_once(session: AsyncSession) -> None:
    await _seed(session, A)
    await _seed(session, B)
    deps = _deps(session)
    await deps.link_guilds(A, B, added_by=9)
    calls = 0
    real = GuildLinkRepository.peers

    async def counting(self: GuildLinkRepository, guild_id: int) -> list[int]:
        nonlocal calls
        calls += 1
        return await real(self, guild_id)

    GuildLinkRepository.peers = counting  # type: ignore[method-assign]
    try:
        for p in range(1, 21):
            await deps.add_guild_hash(A, _gh(p))
    finally:
        GuildLinkRepository.peers = real  # type: ignore[method-assign]
    assert calls == 1
    assert len(await _ids(session, B)) == 20


# --- The campaign sweep's harvest ------------------------------------------


@pytest_asyncio.fixture
async def scope():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as s:
        s.add_all([Guild(guild_id=A), Guild(guild_id=B)])
        await s.commit()
        await GuildLinkRepository(s).link(A, B, added_by=9)
        await s.commit()

    @asynccontextmanager
    async def _scope() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s
            await s.commit()

    yield _scope
    await engine.dispose()


async def test_the_sweep_harvest_reaches_linked_servers(scope) -> None:  # type: ignore[no-untyped-def]
    async with scope() as s:
        s.add(
            Detection(
                guild_id=A,
                channel_id=201,
                message_id=2,
                attachment_id=20,
                uploader_id=42,
                verdict="clean",
                hashes={"phash": 0xAAAA, "dhash": 1, "whash": 2, "ahash": 3},
                idempotency_key="k-2",
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )

    async def _delete(_channel: int, _message: int) -> None:
        return None

    out = await CampaignSweeper(scope, delete_message=_delete).sweep(
        A, uploader_id=42, skip_message_id=1, added_by=0
    )
    assert out.harvested == (f"{0xAAAA:016x}",)
    assert out.linked == (B,)
    async with scope() as s:
        assert await _ids(s, B) == {f"{0xAAAA:016x}"}
