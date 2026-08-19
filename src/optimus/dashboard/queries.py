"""Read-only aggregate queries backing the dashboard pages.

These sit beside (not inside) :mod:`optimus.db.repositories` because they are
presentation-shaped: grouped counts, filtered listings, and cross-guild
rollups that only the dashboard needs. Everything here is strictly SELECT —
phase 1 of the dashboard performs no writes at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.db.models import (
    Detection,
    GlobalHash,
    GlobalHashApproval,
    GlobalTrustedGuild,
    ModAction,
)

CLEAN_VERDICT = "clean"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- Per-guild ---------------------------------------------------------------------


async def verdict_counts(session: AsyncSession, guild_id: int, *, days: int) -> dict[str, int]:
    """Detection counts per verdict for the last ``days`` days."""
    since = _utcnow() - timedelta(days=days)
    stmt = (
        select(Detection.verdict, func.count())
        .where(Detection.guild_id == guild_id, Detection.created_at >= since)
        .group_by(Detection.verdict)
    )
    rows = (await session.execute(stmt)).all()
    return {str(verdict): int(count) for verdict, count in rows}


@dataclass(frozen=True, slots=True)
class DayActivity:
    """One day of scan activity: clean scans vs everything else."""

    day: str
    clean: int
    flagged: int

    @property
    def total(self) -> int:
        return self.clean + self.flagged


async def daily_activity(session: AsyncSession, guild_id: int, *, days: int) -> list[DayActivity]:
    """Per-day clean/flagged scan counts for the last ``days`` days.

    Days with no scans are included as zeros so the chart has a continuous
    axis. ``func.date`` truncates the stored UTC timestamp to a calendar day
    on both SQLite and Postgres.
    """
    now = _utcnow()
    since = now - timedelta(days=days - 1)
    start = datetime(since.year, since.month, since.day, tzinfo=UTC)
    stmt = (
        select(
            func.date(Detection.created_at),
            Detection.verdict,
            func.count(),
        )
        .where(Detection.guild_id == guild_id, Detection.created_at >= start)
        .group_by(func.date(Detection.created_at), Detection.verdict)
    )
    rows = (await session.execute(stmt)).all()
    buckets: dict[str, dict[str, int]] = {}
    for day, verdict, count in rows:
        entry = buckets.setdefault(str(day), {"clean": 0, "flagged": 0})
        key = "clean" if str(verdict) == CLEAN_VERDICT else "flagged"
        entry[key] += int(count)
    out: list[DayActivity] = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).date().isoformat()
        entry = buckets.get(day, {"clean": 0, "flagged": 0})
        out.append(DayActivity(day=day, clean=entry["clean"], flagged=entry["flagged"]))
    return out


async def list_detections(
    session: AsyncSession,
    guild_id: int,
    *,
    verdict: str | None = None,
    uploader_id: int | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[Detection]:
    """Newest-first detections with optional verdict/uploader filters.

    ``before_id`` pages backwards through history: pass the smallest ``id`` of
    the previous page to get the next-older page (keyset pagination — stable
    even while new detections are being written).
    """
    stmt = select(Detection).where(Detection.guild_id == guild_id)
    if verdict is not None:
        stmt = stmt.where(Detection.verdict == verdict)
    if uploader_id is not None:
        stmt = stmt.where(Detection.uploader_id == uploader_id)
    if before_id is not None:
        stmt = stmt.where(Detection.id < before_id)
    stmt = stmt.order_by(Detection.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_detection(
    session: AsyncSession, guild_id: int, detection_id: int
) -> Detection | None:
    """One detection, guild-scoped so cross-guild ids resolve to nothing."""
    stmt = select(Detection).where(Detection.guild_id == guild_id, Detection.id == detection_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_mod_actions(
    session: AsyncSession, guild_id: int, *, limit: int = 100
) -> list[ModAction]:
    """Newest-first audit-log entries for one guild."""
    stmt = (
        select(ModAction)
        .where(ModAction.guild_id == guild_id)
        .order_by(ModAction.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# --- Global (owner-only) --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuildActivityRow:
    """Cross-guild rollup row for the owner overview."""

    guild_id: int
    total: int
    flagged: int


async def guild_overview(session: AsyncSession, *, days: int) -> list[GuildActivityRow]:
    """Per-guild scan totals for the last ``days`` days, busiest first."""
    since = _utcnow() - timedelta(days=days)
    flagged = func.sum(case((Detection.verdict != CLEAN_VERDICT, 1), else_=0))
    stmt = (
        select(Detection.guild_id, func.count(), flagged)
        .where(Detection.created_at >= since)
        .group_by(Detection.guild_id)
        .order_by(func.count().desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        GuildActivityRow(guild_id=int(gid), total=int(total), flagged=int(bad or 0))
        for gid, total, bad in rows
    ]


async def global_hash_status_counts(session: AsyncSession) -> dict[str, int]:
    """How many global hashes exist per status (candidate/promoted/revoked)."""
    stmt = select(GlobalHash.status, func.count()).group_by(GlobalHash.status)
    rows = (await session.execute(stmt)).all()
    return {str(status): int(count) for status, count in rows}


@dataclass(frozen=True, slots=True)
class GlobalHashRow:
    """One global hash plus its approval progress, for the owner queue page."""

    hash: GlobalHash
    votes: int
    distinct_guilds: int


async def list_global_hashes(
    session: AsyncSession, *, status: str, limit: int = 100
) -> list[GlobalHashRow]:
    """Newest-first global hashes in ``status``, with vote/approver counts."""
    stmt = (
        select(
            GlobalHash,
            func.count(GlobalHashApproval.id),
            func.count(func.distinct(GlobalHashApproval.approver_guild_id)),
        )
        .outerjoin(GlobalHashApproval, GlobalHashApproval.hash_id == GlobalHash.hash_id)
        .where(GlobalHash.status == status)
        .group_by(GlobalHash.hash_id)
        .order_by(GlobalHash.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        GlobalHashRow(hash=hash_, votes=int(votes), distinct_guilds=int(guilds))
        for hash_, votes, guilds in rows
    ]


async def list_trusted_guilds(session: AsyncSession) -> list[GlobalTrustedGuild]:
    """All guilds allow-listed to vote on the global database."""
    stmt = select(GlobalTrustedGuild).order_by(GlobalTrustedGuild.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


def summarize_distances(distances: dict[str, Any]) -> str:
    """Compact one-line rendering of a detection's distance/score payload."""
    parts: list[str] = []
    for key in sorted(distances):
        value = distances[key]
        if isinstance(value, float):
            parts.append(f"{key}={value:.3f}")
        else:
            parts.append(f"{key}={value}")
    return ", ".join(parts)
