"""Unit tests for the dashboard's read-only query layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from optimus.dashboard import queries
from optimus.db.models import (
    Detection,
    GlobalHash,
    GlobalHashApproval,
    GlobalTrustedGuild,
    ModAction,
)

GUILD = 101
OTHER_GUILD = 202


def _detection(
    *,
    guild_id: int = GUILD,
    verdict: str = "clean",
    uploader_id: int = 1,
    key: str,
    created_at: datetime | None = None,
) -> Detection:
    row = Detection(
        guild_id=guild_id,
        message_id=1,
        channel_id=2,
        attachment_id=3,
        uploader_id=uploader_id,
        distances={"phash": 4},
        verdict=verdict,
        action_taken="none",
        idempotency_key=key,
    )
    if created_at is not None:
        row.created_at = created_at
    return row


async def _seed_detections(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    session.add_all(
        [
            _detection(key="a", verdict="clean", uploader_id=10),
            _detection(key="b", verdict="scam", uploader_id=10),
            _detection(key="c", verdict="ambiguous", uploader_id=20),
            _detection(
                key="d", verdict="clean", uploader_id=20, created_at=now - timedelta(days=2)
            ),
            # Outside the 30-day window.
            _detection(key="e", verdict="scam", created_at=now - timedelta(days=45)),
            # Another guild entirely.
            _detection(key="f", guild_id=OTHER_GUILD, verdict="scam"),
        ]
    )
    await session.commit()


class TestGuildQueries:
    async def test_verdict_counts_scoped_and_windowed(self, session: AsyncSession) -> None:
        await _seed_detections(session)
        counts = await queries.verdict_counts(session, GUILD, days=30)
        assert counts == {"clean": 2, "scam": 1, "ambiguous": 1}

    async def test_daily_activity_buckets_clean_vs_flagged(self, session: AsyncSession) -> None:
        await _seed_detections(session)
        days = await queries.daily_activity(session, GUILD, days=7)
        assert len(days) == 7
        today = days[-1]
        assert today.clean == 1
        assert today.flagged == 2
        two_ago = days[-3]
        assert (two_ago.clean, two_ago.flagged) == (1, 0)
        assert days[0].total == 0  # padded empty day

    async def test_list_detections_filters_and_pagination(self, session: AsyncSession) -> None:
        await _seed_detections(session)
        # Listing is not time-windowed: all history pages newest-first by id
        # (insertion order in production), including the 45-day-old "e".
        newest_first = await queries.list_detections(session, GUILD)
        assert [d.idempotency_key for d in newest_first] == ["e", "d", "c", "b", "a"]

        scams = await queries.list_detections(session, GUILD, verdict="scam")
        assert [d.idempotency_key for d in scams] == ["e", "b"]

        by_uploader = await queries.list_detections(session, GUILD, uploader_id=20)
        assert {d.idempotency_key for d in by_uploader} == {"c", "d"}

        first_page = await queries.list_detections(session, GUILD, limit=2)
        assert [d.idempotency_key for d in first_page] == ["e", "d"]
        second_page = await queries.list_detections(
            session, GUILD, limit=2, before_id=first_page[-1].id
        )
        assert [d.idempotency_key for d in second_page] == ["c", "b"]

    async def test_get_detection_is_guild_scoped(self, session: AsyncSession) -> None:
        await _seed_detections(session)
        other = await queries.list_detections(session, OTHER_GUILD)
        assert other, "seed created a detection in the other guild"
        stolen = await queries.get_detection(session, GUILD, other[0].id)
        assert stolen is None
        own = await queries.list_detections(session, GUILD, limit=1)
        found = await queries.get_detection(session, GUILD, own[0].id)
        assert found is not None

    async def test_list_mod_actions_newest_first(self, session: AsyncSession) -> None:
        session.add_all(
            [
                ModAction(guild_id=GUILD, actor_id=1, action="config.set", payload={}),
                ModAction(guild_id=GUILD, actor_id=2, action="review.confirm_scam", payload={}),
                ModAction(guild_id=OTHER_GUILD, actor_id=3, action="config.set", payload={}),
            ]
        )
        await session.commit()
        actions = await queries.list_mod_actions(session, GUILD)
        assert [a.action for a in actions] == ["review.confirm_scam", "config.set"]


class TestGlobalQueries:
    async def test_guild_overview_rolls_up_by_guild(self, session: AsyncSession) -> None:
        await _seed_detections(session)
        rows = await queries.guild_overview(session, days=7)
        by_guild = {row.guild_id: row for row in rows}
        assert by_guild[GUILD].total == 4
        assert by_guild[GUILD].flagged == 2
        assert by_guild[OTHER_GUILD].total == 1
        assert by_guild[OTHER_GUILD].flagged == 1
        assert rows[0].guild_id == GUILD  # busiest first

    async def test_global_hash_queue_with_votes(self, session: AsyncSession) -> None:
        session.add_all(
            [
                GlobalHash(hash_id="a" * 16, phash=1, dhash=2, whash=3, status="candidate"),
                GlobalHash(hash_id="b" * 16, phash=4, dhash=5, whash=6, status="promoted"),
                GlobalHashApproval(hash_id="a" * 16, approver_user_id=1, approver_guild_id=11),
                GlobalHashApproval(hash_id="a" * 16, approver_user_id=2, approver_guild_id=11),
            ]
        )
        await session.commit()

        counts = await queries.global_hash_status_counts(session)
        assert counts == {"candidate": 1, "promoted": 1}

        candidates = await queries.list_global_hashes(session, status="candidate")
        assert len(candidates) == 1
        assert candidates[0].votes == 2
        assert candidates[0].distinct_guilds == 1

        promoted = await queries.list_global_hashes(session, status="promoted")
        assert len(promoted) == 1
        assert promoted[0].votes == 0

    async def test_trusted_guild_listing(self, session: AsyncSession) -> None:
        session.add(GlobalTrustedGuild(guild_id=11, added_by=99))
        await session.commit()
        trusted = await queries.list_trusted_guilds(session)
        assert [g.guild_id for g in trusted] == [11]


def test_summarize_distances_is_stable() -> None:
    text = queries.summarize_distances({"phash": 4, "clip": 0.123456})
    assert text == "clip=0.123, phash=4"
