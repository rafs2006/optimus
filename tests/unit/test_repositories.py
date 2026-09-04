"""Repository tests on aiosqlite, focusing on guild_id scoping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from optimus.db.models import (
    Appeal,
    Detection,
    Guild,
    GuildHash,
    GuildTrustedUser,
    GuildWhitelist,
    ModAction,
)
from optimus.db.repositories import (
    DetectionRepository,
    GuildHashRepository,
    GuildPurgeRepository,
    GuildRepository,
    UserOptoutRepository,
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


async def test_set_safe_mode_unknown_guild_auto_provisions(session: AsyncSession) -> None:
    repo = GuildRepository(session)
    await repo.set_safe_mode(999, True)
    guild = await repo.get(999)
    assert guild is not None
    assert guild.safe_mode is True


async def test_get_or_create_returns_existing_row_unchanged(session: AsyncSession) -> None:
    repo = GuildRepository(session)
    await repo.upsert(Guild(guild_id=1, sensitivity="strict"))
    guild = await repo.get_or_create(1)
    assert guild.sensitivity == "strict"


async def test_get_or_create_provisions_default_row_when_missing(session: AsyncSession) -> None:
    repo = GuildRepository(session)
    assert await repo.get(42) is None
    guild = await repo.get_or_create(42)
    assert guild.guild_id == 42
    assert guild.sensitivity == "balanced"
    persisted = await repo.get(42)
    assert persisted is not None


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


async def test_guild_hash_stores_full_range_uint64(session: AsyncSession) -> None:
    # Regression: real perceptual hashes are unsigned 64-bit and routinely have
    # the high bit set (>= 2**63), but the storage column is a *signed* int8.
    # The Uint64 type maps them through two's complement so they round-trip
    # losslessly instead of overflowing on insert.
    await _make_guild(session, 1)
    repo = GuildHashRepository(session, guild_id=1)
    big = (1 << 64) - 1  # max uint64, high bit set
    mid = 1 << 63  # exactly the signed int8 overflow boundary
    await repo.add(GuildHash(hash_id="hi", phash=big, dhash=mid, whash=0, ahash=big))

    session.expire_all()  # force a fresh read through process_result_value
    stored = await repo.get("hi")
    assert stored is not None
    assert stored.phash == big
    assert stored.dhash == mid
    assert stored.whash == 0
    assert stored.ahash == big


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


async def test_detection_belongs_to_enforces_guild_and_uploader(
    session: AsyncSession,
) -> None:
    await _make_guild(session, 5)
    await _make_guild(session, 6)
    repo = DetectionRepository(session, guild_id=5)
    stored = await repo.record(
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

    assert await repo.belongs_to(stored.id, 13) is True
    # Wrong uploader, wrong guild, and unknown id are all rejected.
    assert await repo.belongs_to(stored.id, 99) is False
    assert await DetectionRepository(session, guild_id=6).belongs_to(stored.id, 13) is False
    assert await repo.belongs_to(stored.id + 1000, 13) is False


# --- opt-out / right to erasure ------------------------------------------------


async def test_optout_is_idempotent(session: AsyncSession) -> None:
    repo = UserOptoutRepository(session)
    assert await repo.is_opted_out(42) is False
    await repo.opt_out(42)
    await repo.opt_out(42)  # second call must not raise or duplicate
    assert await repo.is_opted_out(42) is True


async def test_purge_user_removes_rows_but_keeps_tombstone(session: AsyncSession) -> None:
    await _make_guild(session, 1)
    det_repo = DetectionRepository(session, guild_id=1)
    await det_repo.record(
        Detection(
            message_id=1,
            channel_id=2,
            attachment_id=3,
            uploader_id=99,
            distances={},
            verdict="scam",
            idempotency_key="k1",
        )
    )
    session.add(GuildTrustedUser(guild_id=1, user_id=99))
    await session.flush()

    repo = UserOptoutRepository(session)
    await repo.opt_out(99)
    deleted = await repo.purge_user(99)

    assert deleted >= 2
    assert await det_repo.get_by_idempotency_key("k1") is None
    # The opt-out tombstone survives so the user stays excluded.
    assert await repo.is_opted_out(99) is True


# --- full guild erasure (/delete_server_data) ----------------------------------


async def test_guild_purge_removes_every_owned_row(session: AsyncSession) -> None:
    await _make_guild(session, 1)
    await _make_guild(session, 2)
    det_repo = DetectionRepository(session, guild_id=1)
    await det_repo.record(
        Detection(
            message_id=1,
            channel_id=2,
            attachment_id=3,
            uploader_id=5,
            distances={},
            verdict="scam",
            idempotency_key="g1",
        )
    )
    detection = await det_repo.get_by_idempotency_key("g1")
    assert detection is not None
    session.add(Appeal(guild_id=1, detection_id=detection.id, user_id=5))
    session.add(ModAction(guild_id=1, actor_id=7, action="ban"))
    session.add(GuildHash(hash_id="h", phash=1, dhash=1, whash=1, guild_id=1))
    # A second guild's data must be untouched.
    other = DetectionRepository(session, guild_id=2)
    await other.record(
        Detection(
            message_id=9,
            channel_id=9,
            attachment_id=9,
            uploader_id=9,
            distances={},
            verdict="scam",
            idempotency_key="keep",
        )
    )
    await session.flush()

    deleted = await GuildPurgeRepository(session, guild_id=1).purge()

    assert deleted >= 4
    assert await GuildRepository(session).get(1) is None
    assert await det_repo.get_by_idempotency_key("g1") is None
    # Guild 2 is left intact.
    assert await GuildRepository(session).get(2) is not None
    assert await other.get_by_idempotency_key("keep") is not None


async def test_deployment_boot_counter_grows_first_boot_stays(session: AsyncSession) -> None:
    from datetime import UTC, datetime

    from optimus.db.repositories import DeploymentBootRepository

    repo = DeploymentBootRepository(session)
    empty = await repo.summary()
    assert empty.boots == 0 and empty.first_boot_at is None

    first = await repo.record_boot(now=datetime(2026, 8, 1, tzinfo=UTC))
    assert first.boots == 1

    second = await repo.record_boot(now=datetime(2026, 8, 17, tzinfo=UTC))
    assert second.boots == 2
    # The persistence canary: the first-boot timestamp never moves.
    assert second.first_boot_at is not None
    assert second.first_boot_at.replace(tzinfo=UTC) == datetime(2026, 8, 1, tzinfo=UTC)


async def test_global_trusted_guild_repo_roundtrip(session: AsyncSession) -> None:
    from optimus.db.repositories import GlobalTrustedGuildRepository

    repo = GlobalTrustedGuildRepository(session)
    assert await repo.contains(123) is False
    assert await repo.list_all() == []

    assert await repo.add(123, added_by=1) is True
    assert await repo.add(123, added_by=2) is False  # idempotent
    assert await repo.contains(123) is True
    await repo.add(456, added_by=1)
    assert {row.guild_id for row in await repo.list_all()} == {123, 456}

    assert await repo.remove(123) is True
    assert await repo.remove(123) is False  # already gone
    assert await repo.contains(123) is False
    assert [row.guild_id for row in await repo.list_all()] == [456]


async def _record_detection(
    repo: DetectionRepository, *, message_id: int, created_at: datetime
) -> Detection:
    return await repo.record(
        Detection(
            message_id=message_id,
            channel_id=11,
            attachment_id=message_id,
            uploader_id=13,
            distances={"phash": 0},
            verdict="scam",
            idempotency_key=f"{message_id}:{message_id}",
            created_at=created_at,
        )
    )


async def test_list_open_returns_only_posted_and_unactioned_rows(
    session: AsyncSession,
) -> None:
    """The queue exists to be drainable, so every terminal state must leave it."""
    await _make_guild(session, 5)
    repo = DetectionRepository(session, guild_id=5)
    base = datetime(2026, 1, 1, tzinfo=UTC)

    waiting = await _record_detection(repo, message_id=1, created_at=base)
    never_posted = await _record_detection(repo, message_id=2, created_at=base)
    dismissed = await _record_detection(repo, message_id=3, created_at=base)
    confirmed = await _record_detection(repo, message_id=4, created_at=base)

    for det in (waiting, dismissed, confirmed):
        await repo.set_reported_at(det.id, base)
    await repo.set_action_taken(dismissed.id, "dismissed")
    await repo.set_action_taken(confirmed.id, "confirmed")

    open_ids = [d.id for d in await repo.list_open(limit=25)]
    assert open_ids == [waiting.id]
    # A row with no card posted belongs to the /setup replay, not the queue.
    assert never_posted.id not in open_ids
    assert await repo.count_open() == 1


async def test_list_open_is_oldest_first_scoped_and_capped(session: AsyncSession) -> None:
    await _make_guild(session, 5)
    await _make_guild(session, 6)
    repo = DetectionRepository(session, guild_id=5)
    base = datetime(2026, 1, 1, tzinfo=UTC)

    # Recorded newest-first so ordering cannot pass by insertion accident.
    ids = []
    for offset in (2, 1, 0):
        det = await _record_detection(
            repo, message_id=100 + offset, created_at=base + timedelta(hours=offset)
        )
        await repo.set_reported_at(det.id, base)
        ids.append((offset, det.id))
    expected = [det_id for _, det_id in sorted(ids)]

    assert [d.id for d in await repo.list_open(limit=25)] == expected
    assert [d.id for d in await repo.list_open(limit=2)] == expected[:2]
    # The cap must not distort the total a moderator is shown.
    assert await repo.count_open() == 3

    other = DetectionRepository(session, guild_id=6)
    assert await other.list_open(limit=25) == []
    assert await other.count_open() == 0
