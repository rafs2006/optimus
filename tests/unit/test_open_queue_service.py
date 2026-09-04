"""``DbDeps.open_queue`` against a real aiosqlite session.

The handler tests use a fake, so the parts that can only break against a real
database live here: aiosqlite strips ``tzinfo`` on datetime round-trip, so the
age arithmetic subtracts a naive value from an aware ``now`` unless the service
re-attaches UTC -- which raises ``TypeError`` rather than returning a wrong
number. The cap-versus-total split is also verified end to end, because a total
that shrinks to the page size would quietly tell moderators the backlog is
smaller than it is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from optimus.core.config import get_settings
from optimus.core.ratelimit import RateLimit
from optimus.db.models import Detection
from optimus.services.interactions.service import DbDeps

GUILD_ID = 424242424242424242
CHANNEL_ID = 555555555555555555


class _NoopRateLimiter:
    async def acquire(self, key: str, limit: RateLimit, cost: float = 1.0) -> bool:
        return True


def _make_deps(session: AsyncSession) -> DbDeps:
    return DbDeps(session, _NoopRateLimiter(), get_settings())  # type: ignore[arg-type]


async def _open_detection(
    session: AsyncSession, *, key: str, age: timedelta, reported: bool = True
) -> Detection:
    now = datetime.now(UTC)
    detection = Detection(
        guild_id=GUILD_ID,
        message_id=abs(hash(key)) % 10**9,
        channel_id=CHANNEL_ID,
        attachment_id=1,
        uploader_id=42,
        distances={},
        hashes=None,
        verdict="scam",
        idempotency_key=key,
        created_at=now - age,
        reported_at=now - age if reported else None,
    )
    session.add(detection)
    await session.flush()
    return detection


async def test_open_queue_computes_ages_across_the_tz_round_trip(
    session: AsyncSession,
) -> None:
    await _open_detection(session, key="a", age=timedelta(hours=3))
    summary = await _make_deps(session).open_queue(GUILD_ID, limit=25)

    assert summary["total"] == 1
    (row,) = summary["rows"]
    # ~3h, with slack for test execution time. The real assertion is that this
    # returned a number at all: a naive/aware subtraction raises TypeError.
    assert 10_700 < row["age_seconds"] < 10_900
    assert row["channel_id"] == CHANNEL_ID
    assert row["uploader_id"] == 42
    assert row["verdict"] == "scam"


async def test_open_queue_total_counts_beyond_the_page(session: AsyncSession) -> None:
    for i in range(4):
        await _open_detection(session, key=f"row-{i}", age=timedelta(minutes=10 * (4 - i)))
    # A card that never posted must not inflate either number.
    await _open_detection(session, key="unposted", age=timedelta(hours=1), reported=False)

    summary = await _make_deps(session).open_queue(GUILD_ID, limit=2)

    # The window count sees every qualifying row even though LIMIT cut the page.
    assert summary["total"] == 4
    assert len(summary["rows"]) == 2
    # Oldest first survives the service layer, not just the repository.
    ages = [row["age_seconds"] for row in summary["rows"]]
    assert ages == sorted(ages, reverse=True)
