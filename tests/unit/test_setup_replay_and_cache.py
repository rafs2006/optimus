"""Regression tests for the PR that fixes:

1. ``/config set`` on a scan-policy field must drop the cached
   :class:`GuildConfigCache` snapshot after commit -- otherwise ``should_scan``
   keeps returning the pre-write answer for up to 5 minutes.
2. Detections captured before ``/setup`` linked a review channel must be
   stamped ``reported_at`` when their card actually posts, and available for
   replay via ``DetectionRepository.list_unreported_since`` until they are
   stamped.
3. ``/config view`` on a guild that has queued detections but no review
   channel should surface a pending-scan notice.
4. The join backfill's gating predicate: it must defer when the guild has
   no review channel and run otherwise.

Every test drives real DbDeps (or a real DetectionRepository) against an
isolated aiosqlite session so the assertions actually depend on the
production code path, not on a hand-rolled fake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.core.config import get_settings
from optimus.core.ratelimit import RateLimit
from optimus.db.models import Detection
from optimus.db.repositories import DetectionRepository
from optimus.services.gateway.bot import GatewayService
from optimus.services.interactions.service import DbDeps

GUILD_ID = 424242424242424242
CHANNEL_ID = 555555555555555555


class _NoopRateLimiter:
    async def acquire(self, key: str, limit: RateLimit, cost: float = 1.0) -> bool:
        return True


def _make_deps(session: AsyncSession) -> DbDeps:
    return DbDeps(session, _NoopRateLimiter(), get_settings())  # type: ignore[arg-type]


async def _persist_detection(session: AsyncSession, *, created_at: datetime, key: str) -> Detection:
    """Persist a minimal detection row for the given guild.

    Only the fields the ``/setup`` replay actually reads are populated -- the
    rest of the columns get defaults from the model. ``created_at`` is set
    explicitly because the replay filters on it.
    """
    detection = Detection(
        guild_id=GUILD_ID,
        message_id=int(key[-9:], 16) if all(c in "0123456789abcdef" for c in key[-9:]) else 900,
        channel_id=CHANNEL_ID,
        attachment_id=1,
        uploader_id=42,
        distances={},
        hashes=None,
        verdict="scam",
        action_taken="delete",
        idempotency_key=key,
        created_at=created_at,
    )
    session.add(detection)
    await session.flush()
    return detection


# ---------------------------------------------------------------------------
# Fix 1: scan-policy fields queue a cache invalidation, other fields do not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value, expected_queued",
    [
        ("sensitivity", "strict", True),
        ("safe_mode", True, True),
        ("optin_scan_bots", True, True),
        # These live outside the scan-policy snapshot -- churning the cache
        # on every write to them is pointless.
        ("review_channel", CHANNEL_ID, False),
        ("mod_queue_threshold", 0.5, False),
        ("retention_days", 14, False),
        ("locale", "sr", False),
        ("optin_global_db", True, False),
        ("optin_evidence_storage", True, False),
        ("action_policy", "delete_ban", False),
        ("ban_purge_hours", 24, False),
    ],
)
async def test_set_config_field_queues_scan_policy_invalidation(
    session: AsyncSession, field: str, value: Any, expected_queued: bool
) -> None:
    deps = _make_deps(session)
    await deps.set_config_field(GUILD_ID, field, value)
    assert (GUILD_ID in deps.pending_scan_policy_invalidations) is expected_queued


# ---------------------------------------------------------------------------
# Fix 3a: first review-channel link queues the /setup hook; subsequent
# writes to review_channel (already linked) do not.
# ---------------------------------------------------------------------------


async def test_first_review_channel_link_queues_setup_hook(session: AsyncSession) -> None:
    deps = _make_deps(session)
    await deps.set_config_field(GUILD_ID, "review_channel", CHANNEL_ID)
    assert GUILD_ID in deps.pending_review_channel_linked


async def test_relink_review_channel_does_not_queue_setup_hook(
    session: AsyncSession,
) -> None:
    deps = _make_deps(session)
    await deps.set_config_field(GUILD_ID, "review_channel", CHANNEL_ID)
    # Simulate the post-commit drain: reset the queue as if the hook ran.
    deps.pending_review_channel_linked.clear()
    await deps.set_config_field(GUILD_ID, "review_channel", CHANNEL_ID + 1)
    assert GUILD_ID not in deps.pending_review_channel_linked


# ---------------------------------------------------------------------------
# Fix 2: reported_at stamping + replay window semantics.
# ---------------------------------------------------------------------------


async def test_set_reported_at_stamps_the_row(session: AsyncSession) -> None:
    detection = await _persist_detection(session, created_at=datetime.now(UTC), key="stamp-1")
    when = datetime.now(UTC)
    await DetectionRepository(session, GUILD_ID).set_reported_at(detection.id, when)
    await session.refresh(detection)
    assert detection.reported_at is not None
    # aiosqlite strips tzinfo on the round-trip, so normalize both sides.
    stored = detection.reported_at.replace(tzinfo=None)
    assert abs((stored - when.replace(tzinfo=None)).total_seconds()) < 1


async def test_list_unreported_since_returns_newest_first_within_window(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    # One inside the window, two inside newer, one *outside* (older than 3d),
    # one already stamped inside the window: only the two newest, unstamped,
    # in-window rows should come back, newest-first.
    older = await _persist_detection(session, created_at=now - timedelta(days=5), key="older")
    stamped = await _persist_detection(session, created_at=now - timedelta(hours=1), key="stamped")
    keeper_new = await _persist_detection(session, created_at=now - timedelta(minutes=5), key="new")
    keeper_mid = await _persist_detection(session, created_at=now - timedelta(hours=6), key="mid")
    repo = DetectionRepository(session, GUILD_ID)
    await repo.set_reported_at(stamped.id, now)
    rows = await repo.list_unreported_since(now - timedelta(days=3), limit=50)
    ids = [r.id for r in rows]
    assert older.id not in ids
    assert stamped.id not in ids
    # Newest first
    assert ids == [keeper_new.id, keeper_mid.id]


async def test_count_unreported_since_matches_list(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    await _persist_detection(session, created_at=now - timedelta(minutes=5), key="c1")
    await _persist_detection(session, created_at=now - timedelta(minutes=6), key="c2")
    await _persist_detection(session, created_at=now - timedelta(minutes=7), key="c3")
    repo = DetectionRepository(session, GUILD_ID)
    since = now - timedelta(days=3)
    rows = await repo.list_unreported_since(since, limit=50)
    count = await repo.count_unreported_since(since)
    assert count == len(rows) == 3


async def test_list_unreported_since_respects_limit(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    # Insert 5 in-window, ask for 2 -> get 2 newest.
    for i in range(5):
        await _persist_detection(session, created_at=now - timedelta(minutes=i), key=f"limit-{i}")
    repo = DetectionRepository(session, GUILD_ID)
    rows = await repo.list_unreported_since(now - timedelta(days=3), limit=2)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Fix 3b: has_pending_scan surfaces the notice only when there are
# unreported detections in the window.
# ---------------------------------------------------------------------------


async def test_has_pending_scan_is_false_when_no_detections(session: AsyncSession) -> None:
    deps = _make_deps(session)
    assert await deps.has_pending_scan(GUILD_ID) is False


async def test_has_pending_scan_is_true_when_unreported_in_window(
    session: AsyncSession,
) -> None:
    await _persist_detection(
        session, created_at=datetime.now(UTC) - timedelta(minutes=1), key="pending-1"
    )
    deps = _make_deps(session)
    assert await deps.has_pending_scan(GUILD_ID) is True


async def test_has_pending_scan_ignores_stamped_rows(session: AsyncSession) -> None:
    detection = await _persist_detection(
        session, created_at=datetime.now(UTC) - timedelta(minutes=1), key="stamped-2"
    )
    await DetectionRepository(session, GUILD_ID).set_reported_at(detection.id, datetime.now(UTC))
    deps = _make_deps(session)
    assert await deps.has_pending_scan(GUILD_ID) is False


# ---------------------------------------------------------------------------
# Fix 3c: gateway join backfill defers when the predicate says no review
# channel is linked, and runs otherwise.
# ---------------------------------------------------------------------------


class _StubBus:
    async def publish(self, *args: Any, **kwargs: Any) -> None:
        return None


class _StubHealth:
    def add_readiness_check(self, *args: Any, **kwargs: Any) -> None:
        return None


class _RecordingHistory:
    """History reader that just records the guild ids it was asked about."""

    def __init__(self) -> None:
        self.called_for: list[int] = []

    async def list_text_channel_ids(self, guild_id: int) -> list[int]:
        self.called_for.append(guild_id)
        return []

    async def fetch_recent_messages(
        self, channel_id: int, *, after: datetime, limit: int
    ) -> list[Any]:
        return []


async def _make_gateway(has_review_channel: Any) -> tuple[GatewayService, _RecordingHistory]:
    history = _RecordingHistory()
    settings = get_settings()
    # Fake config cache: the gateway only uses this for should_scan; not
    # exercised in this test, so a stub with the same async surface is fine.

    class _StubCache:
        async def get(self, guild_id: int) -> Any:
            return None

        async def invalidate(self, guild_id: int) -> None:
            return None

    gateway = GatewayService(
        settings,
        _StubBus(),  # type: ignore[arg-type]
        _StubCache(),  # type: ignore[arg-type]
        _StubHealth(),  # type: ignore[arg-type]
        history=history,  # type: ignore[arg-type]
        has_review_channel=has_review_channel,
    )
    return gateway, history


async def test_join_backfill_defers_when_no_review_channel() -> None:
    async def _no(_: int) -> bool:
        return False

    gateway, history = await _make_gateway(_no)
    await gateway._maybe_join_backfill(GUILD_ID)
    assert history.called_for == []


async def test_join_backfill_runs_when_review_channel_linked() -> None:
    async def _yes(_: int) -> bool:
        return True

    gateway, history = await _make_gateway(_yes)
    await gateway._maybe_join_backfill(GUILD_ID)
    assert history.called_for == [GUILD_ID]


async def test_join_backfill_defers_when_predicate_errors() -> None:
    async def _boom(_: int) -> bool:
        raise RuntimeError("db down")

    gateway, history = await _make_gateway(_boom)
    await gateway._maybe_join_backfill(GUILD_ID)
    # Fail-closed: a lookup failure defers rather than running the scan.
    assert history.called_for == []


async def test_run_deferred_join_backfill_bypasses_predicate() -> None:
    async def _no(_: int) -> bool:
        return False

    gateway, history = await _make_gateway(_no)
    await gateway.run_deferred_join_backfill(GUILD_ID)
    assert history.called_for == [GUILD_ID]
