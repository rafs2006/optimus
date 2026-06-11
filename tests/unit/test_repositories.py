"""Repository tests on aiosqlite, focusing on guild_id scoping."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.db.models import Detection, Guild, GuildHash, GuildWhitelist
from optimus.db.repositories import (
    DetectionRepository,
    GuildHashRepository,
    GuildRepository,
    WhitelistRepository,
)


async def _make_guild(session: AsyncSession, guild_id: int) -> None:
    repo = GuildRepository(session)
    await repo.upsert(Guild(guild_id=guild_id))


async def test_guild_upsert_and_safe_mode(session: AsyncSession) -> None:
    repo = GuildRepository(session)
    await repo.upsert(Guild(guild_id=1, sensitivity="strict"))
    guild = await repo.get(1)
    assert guild is not None
    assert guild.sensitivity == "strict"
    assert guild.safe_mode is False
    await repo.set_safe_mode(1, True)
    refreshed = await repo.get(1)
    assert refreshed is not None and refreshed.safe_mode is True


async def test_set_safe_mode_unknown_guild_raises(session: AsyncSession) -> None:
    repo = GuildRepository(session)
    with pytest.raises(KeyError):
        await repo.set_safe_mode(999, True)


async def test_guild_hash_repo_scopes_to_guild(session: AsyncSession) -> None:
    await _make_guild(session, 1)
    await _make_guild(session, 2)

    repo1 = GuildHashRepository(session, guild_id=1)
    repo2 = GuildHashRepository(session, guild_id=2)

    await repo1.add(GuildHash(hash_id="h1", phash=1, dhash=2, whash=3, guild_id=999))
    await repo2.add(GuildHash(hash_id="h2", phash=4, dhash=5, whash=6))

    # add() forces the repository's guild scope regardless of the passed value.
    stored = await repo1.get("h1")
    assert stored is not None and stored.guild_id == 1

    # Cross-guild reads return nothing.
    assert await repo1.get("h2") is None
    assert await repo2.get("h1") is None

    active1 = await repo1.list_active()
    assert [h.hash_id for h in active1] == ["h1"]


async def test_guild_hash_remove_is_scoped(session: AsyncSession) -> None:
    await _make_guild(session, 1)
    await _make_guild(session, 2)
    repo1 = GuildHashRepository(session, guild_id=1)
    repo2 = GuildHashRepository(session, guild_id=2)
    await repo1.add(GuildHash(hash_id="x", phash=1, dhash=1, whash=1))

    # Removing from the wrong guild scope deletes nothing.
    assert await repo2.remove("x") == 0
    assert await repo1.remove("x") == 1
    assert await repo1.get("x") is None


async def test_whitelist_scoped(session: AsyncSession) -> None:
    await _make_guild(session, 7)
    repo = WhitelistRepository(session, guild_id=7)
    await repo.add(GuildWhitelist(phash=1, dhash=2, whash=3, reason="legit", guild_id=999))
    entries = await repo.list()
    assert len(entries) == 1
    assert entries[0].guild_id == 7


async def test_detection_idempotency_scoped(session: AsyncSession) -> None:
    await _make_guild(session, 5)
    repo = DetectionRepository(session, guild_id=5)
    await repo.record(
        Detection(
            message_id=10,
            channel_id=11,
            attachment_id=12,
            uploader_id=13,
            distances={"phash": 0},
            verdict="scam",
            idempotency_key="10:12",
        )
    )
    found = await repo.get_by_idempotency_key("10:12")
    assert found is not None and found.guild_id == 5

    other = DetectionRepository(session, guild_id=6)
    assert await other.get_by_idempotency_key("10:12") is None

    recent = await repo.list_recent()
    assert len(recent) == 1
