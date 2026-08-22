"""Tests for the cross-channel campaign sweep.

The incident these cover: a scammer posted the same image into many channels,
the bot deleted one message, never banned, and the copies survived.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.db.engine import create_engine, create_session_factory
from optimus.db.models import Base, Detection, Guild
from optimus.db.repositories import GuildHashRepository
from optimus.services.moderation.sweep import CampaignSweeper

GUILD = 1
SCAMMER = 42

# A distinct 4-hash ensemble per image; only phash keys the blocklist entry.
_IMAGE_A = {"phash": 0xAAAA, "dhash": 1, "whash": 2, "ahash": 3}
_IMAGE_B = {"phash": 0xBBBB, "dhash": 4, "whash": 5, "ahash": 6}


@pytest_asyncio.fixture
async def scope():  # type: ignore[no-untyped-def]
    """A session-scope factory over one in-memory DB, as the app supplies."""
    engine = create_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    # guild_hashes carries a FK to guilds, so the harvest needs a real guild row.
    async with factory() as session:
        session.add(Guild(guild_id=GUILD))
        await session.commit()

    @asynccontextmanager
    async def _scope() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session
            await session.commit()

    yield _scope
    await engine.dispose()


class _Deleter:
    """Records deletions; can be told to fail for specific messages."""

    def __init__(self, fail: set[int] | None = None) -> None:
        self.deleted: list[tuple[int, int]] = []
        self._fail = fail or set()

    async def __call__(self, channel_id: int, message_id: int) -> None:
        if message_id in self._fail:
            raise RuntimeError("missing access")
        self.deleted.append((channel_id, message_id))


async def _seed(
    scope,  # type: ignore[no-untyped-def]
    rows: list[tuple[int, int, dict[str, int]]],
    *,
    uploader: int = SCAMMER,
    age: timedelta = timedelta(minutes=5),
) -> None:
    async with scope() as session:
        for channel_id, message_id, hashes in rows:
            session.add(
                Detection(
                    guild_id=GUILD,
                    channel_id=channel_id,
                    message_id=message_id,
                    attachment_id=message_id * 10,
                    uploader_id=uploader,
                    verdict="clean",
                    hashes=dict(hashes),
                    idempotency_key=f"k-{channel_id}-{message_id}",
                    created_at=datetime.now(UTC) - age,
                )
            )


@pytest.mark.asyncio
async def test_sweep_deletes_copies_in_every_channel(scope) -> None:  # type: ignore[no-untyped-def]
    """The core incident: copies across other channels must all come down."""
    await _seed(
        scope,
        [(201, 1, _IMAGE_A), (202, 2, _IMAGE_A), (203, 3, _IMAGE_A), (204, 4, _IMAGE_A)],
    )
    deleter = _Deleter()
    sweeper = CampaignSweeper(scope, delete_message=deleter)

    out = await sweeper.sweep(GUILD, uploader_id=SCAMMER, skip_message_id=1, added_by=0)

    # Message 1 is the one the caller already handled; the other three are the
    # copies that used to survive.
    assert out.deleted == 3
    assert out.channels == 3
    assert sorted(m for _, m in deleter.deleted) == [2, 3, 4]
    assert (201, 1) not in deleter.deleted


@pytest.mark.asyncio
async def test_sweep_harvests_distinct_variants_into_blocklist(scope) -> None:  # type: ignore[no-untyped-def]
    """Varied images across channels each become their own blocklist entry."""
    await _seed(scope, [(201, 1, _IMAGE_A), (202, 2, _IMAGE_B), (203, 3, _IMAGE_A)])
    sweeper = CampaignSweeper(scope, delete_message=_Deleter())

    out = await sweeper.sweep(GUILD, uploader_id=SCAMMER, skip_message_id=99, added_by=7)

    # Two distinct images -> two hashes; the repeated one collapses.
    assert sorted(out.harvested) == [f"{0xAAAA:016x}", f"{0xBBBB:016x}"]
    async with scope() as session:
        rows = await GuildHashRepository(session, GUILD).list_active()
    assert {r.source for r in rows} == {"campaign_sweep"}
    assert {r.added_by for r in rows} == {7}


@pytest.mark.asyncio
async def test_sweep_ignores_other_uploaders_and_other_guilds(scope) -> None:  # type: ignore[no-untyped-def]
    """A sweep must never touch messages the confirmed scammer did not post."""
    await _seed(scope, [(201, 1, _IMAGE_A)])
    await _seed(scope, [(202, 2, _IMAGE_A)], uploader=777)
    deleter = _Deleter()
    sweeper = CampaignSweeper(scope, delete_message=deleter)

    out = await sweeper.sweep(GUILD, uploader_id=SCAMMER, skip_message_id=99, added_by=0)

    assert out.deleted == 1
    assert deleter.deleted == [(201, 1)]


@pytest.mark.asyncio
async def test_sweep_respects_time_window(scope) -> None:  # type: ignore[no-untyped-def]
    """Old history is out of scope; only the recent spam run is swept."""
    await _seed(scope, [(201, 1, _IMAGE_A)], age=timedelta(days=30))
    deleter = _Deleter()
    sweeper = CampaignSweeper(scope, delete_message=deleter, window_hours=24)

    out = await sweeper.sweep(GUILD, uploader_id=SCAMMER, skip_message_id=99, added_by=0)

    assert out.deleted == 0
    assert deleter.deleted == []


@pytest.mark.asyncio
async def test_sweep_continues_past_individual_delete_failures(scope) -> None:  # type: ignore[no-untyped-def]
    """One unreachable channel must not abandon the rest of the campaign."""
    await _seed(scope, [(201, 1, _IMAGE_A), (202, 2, _IMAGE_A), (203, 3, _IMAGE_A)])
    deleter = _Deleter(fail={2})
    sweeper = CampaignSweeper(scope, delete_message=deleter)

    out = await sweeper.sweep(GUILD, uploader_id=SCAMMER, skip_message_id=99, added_by=0)

    assert out.deleted == 2
    assert out.failed == 1
    # The hash is still harvested even though its message could not be removed.
    assert out.harvested == (f"{0xAAAA:016x}",)


@pytest.mark.asyncio
async def test_sweep_is_noop_without_matching_rows(scope) -> None:  # type: ignore[no-untyped-def]
    sweeper = CampaignSweeper(scope, delete_message=_Deleter())
    out = await sweeper.sweep(GUILD, uploader_id=SCAMMER, skip_message_id=1, added_by=0)
    assert not out.touched


@pytest.mark.asyncio
async def test_sweep_does_not_overwrite_an_existing_hash(scope) -> None:  # type: ignore[no-untyped-def]
    """A moderator's manual entry keeps its attribution."""
    from optimus.db.models import GuildHash

    async with scope() as session:
        await GuildHashRepository(session, GUILD).add(
            GuildHash(
                hash_id=f"{0xAAAA:016x}",
                phash=_IMAGE_A["phash"],
                dhash=_IMAGE_A["dhash"],
                whash=_IMAGE_A["whash"],
                ahash=_IMAGE_A["ahash"],
                source="manual",
                added_by=555,
            )
        )
    await _seed(scope, [(201, 1, _IMAGE_A)])
    sweeper = CampaignSweeper(scope, delete_message=_Deleter())

    out = await sweeper.sweep(GUILD, uploader_id=SCAMMER, skip_message_id=99, added_by=7)

    assert out.harvested == ()
    async with scope() as session:
        rows = await GuildHashRepository(session, GUILD).list_active()
    assert [(r.source, r.added_by) for r in rows] == [("manual", 555)]


@pytest.mark.asyncio
async def test_sweep_deletes_each_message_once_for_multi_attachment_posts(scope) -> None:  # type: ignore[no-untyped-def]
    """Several detections for one message must not mean several deletions."""
    await _seed(scope, [(201, 1, _IMAGE_A)])
    async with scope() as session:
        session.add(
            Detection(
                guild_id=GUILD,
                channel_id=201,
                message_id=1,
                attachment_id=999,
                uploader_id=SCAMMER,
                verdict="clean",
                hashes=dict(_IMAGE_B),
                idempotency_key="k-second-attachment",
                created_at=datetime.now(UTC),
            )
        )
    deleter = _Deleter()
    sweeper = CampaignSweeper(scope, delete_message=deleter)

    out = await sweeper.sweep(GUILD, uploader_id=SCAMMER, skip_message_id=99, added_by=0)

    assert out.deleted == 1
    assert deleter.deleted == [(201, 1)]
    # Both images on that message are still harvested.
    assert len(out.harvested) == 2
