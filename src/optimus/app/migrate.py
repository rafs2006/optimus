"""Run database migrations programmatically on simple-mode startup.

Simple mode points at a local SQLite file that almost never exists yet, so the
process brings the schema up to date itself rather than making the operator run
``alembic upgrade`` by hand. When the ``migrations/`` tree is on disk (a source
checkout — the common case for getting started) we run the real Alembic upgrade
so simple and distributed share one schema source of truth. If it is absent (an
installed wheel that did not ship the migration scripts) we fall back to creating
the schema straight from the SQLAlchemy metadata, which is equivalent for the
fresh SQLite database simple mode targets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from optimus.core.logging import get_logger

_log = get_logger(__name__)


def _find_alembic_ini() -> Path | None:
    """Locate ``alembic.ini`` by walking up from this file to the repo root."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "alembic.ini"
        if candidate.is_file() and (parent / "migrations").is_dir():
            return candidate
    return None


def _alembic_upgrade(ini_path: Path, url: str) -> None:
    """Run a synchronous Alembic ``upgrade head`` against ``url``."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(ini_path))
    # alembic.ini uses a relative script_location; anchor it at the repo root so
    # the upgrade works regardless of the process's current directory.
    config.set_main_option("script_location", str(ini_path.parent / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


async def _create_all(url: str) -> None:
    """Create the full schema from metadata (installed-wheel fallback)."""
    from optimus.db.engine import create_engine
    from optimus.db.models import Base

    engine = create_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def run_migrations(url: str) -> None:
    """Bring the database at ``url`` up to the latest schema.

    Prefers a real Alembic upgrade (run in a worker thread, since Alembic's
    command API is synchronous); falls back to ``metadata.create_all`` when the
    migration scripts are not on disk.
    """
    ini_path = _find_alembic_ini()
    if ini_path is not None:
        _log.info("running_migrations", backend="alembic", url=_redact(url))
        await asyncio.to_thread(_alembic_upgrade, ini_path, url)
        return
    _log.info("running_migrations", backend="create_all", url=_redact(url))
    await _create_all(url)


def _redact(url: str) -> str:
    """Strip any credentials from a DB URL before logging it."""
    if "@" in url:
        scheme, _, tail = url.partition("://")
        return f"{scheme}://***@{tail.split('@', 1)[-1]}"
    return url
