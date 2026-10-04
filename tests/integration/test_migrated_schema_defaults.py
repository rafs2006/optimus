"""The ORM must be able to insert into the schema the migrations really build.

Unit tests create tables from the models (``Base.metadata.create_all``), which
carry every ``server_default``. Production runs the Alembic migrations, which
only carry what each migration declared. Migration 0009 created
``global_trusted_guilds.created_at`` as ``NOT NULL`` without a default, so the
ORM -- relying on a default that existed only in the models -- failed every
``/global approve_server`` in production while the whole test suite passed.

These tests run against a database built by ``alembic upgrade head`` on a real
SQLite file, exactly like a deploy, and pin two things: every column the
models expect the database to fill is filled by the application too, and the
owner's server-approval round trip works end to end.
"""

from __future__ import annotations

import sqlite3
import warnings
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.db.engine import create_engine, create_session_factory
from optimus.db.models import Base
from optimus.db.repositories import GlobalTrustedGuildRepository

ROOT = Path(__file__).resolve().parents[2]


def _migrate(db: Path) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        command.upgrade(cfg, "head")


def _db_has_default(c: sqlite3.Connection, table: str, column: str) -> bool:
    for _cid, name, _type, _notnull, default, _pk in c.execute(f"pragma table_info({table})"):
        if name == column:
            return default is not None
    raise AssertionError(f"{table}.{column} is missing from the migrated schema")


def test_every_database_filled_column_is_also_filled_by_the_app(tmp_path: Path) -> None:
    """A column the models leave to the database must not depend on it.

    For each model column with a ``server_default`` that the migrated table
    lacks, the model must supply a client-side ``default`` -- otherwise the
    ORM omits the column and the insert fails, as approve_server did.
    """
    db = tmp_path / "optimus.db"
    _migrate(db)
    missing: list[str] = []
    with sqlite3.connect(db) as c:
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if column.server_default is None or column.nullable:
                    continue
                if _db_has_default(c, table.name, column.name):
                    continue
                if column.default is None:
                    missing.append(f"{table.name}.{column.name}")
    assert missing == []


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    # Alembic's env runs its own event loop, so migrate before the async test.
    db = tmp_path / "optimus.db"
    _migrate(db)
    return db


@pytest_asyncio.fixture
async def migrated(migrated_db: Path) -> AsyncIterator[AsyncSession]:
    db = migrated_db
    engine = create_engine(f"sqlite+aiosqlite:///{db}")
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_approve_server_round_trip_on_the_migrated_schema(migrated: AsyncSession) -> None:
    repo = GlobalTrustedGuildRepository(migrated)
    assert await repo.add(1111, added_by=42) is True
    assert await repo.add(1111, added_by=42) is False  # already approved
    await migrated.commit()

    (row,) = await repo.list_all()
    assert row.guild_id == 1111
    assert row.added_by == 42
    assert row.created_at is not None
    assert await repo.contains(1111)

    assert await repo.remove(1111) is True
    await migrated.commit()
    assert await repo.list_all() == []


async def test_two_servers_can_be_approved(migrated: AsyncSession) -> None:
    repo = GlobalTrustedGuildRepository(migrated)
    for guild_id in (2222, 3333):
        assert await repo.add(guild_id, added_by=42) is True
    await migrated.commit()
    assert sorted(r.guild_id for r in await repo.list_all()) == [2222, 3333]
