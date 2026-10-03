"""Migration 0011 re-keys guild_hashes per server without losing anything.

Production runs SQLite, where a primary key cannot be altered in place: batch
mode rebuilds the table and copies every row. This upgrades a real database
file holding rows, then checks the new key, every row and column, the guilds
foreign key (with ON DELETE CASCADE) and the downgrade. Warnings are errors,
so an SQLAlchemy upgrade that turns the batch rebuild's warning into a failure
shows up here rather than on deploy.
"""

from __future__ import annotations

import sqlite3
import warnings
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]


def _cfg(db: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    return cfg


def _pk(c: sqlite3.Connection) -> list[str]:
    rows = [r for r in c.execute("pragma table_info(guild_hashes)") if r[5]]
    return [r[1] for r in sorted(rows, key=lambda r: r[5])]


def _snapshot(c: sqlite3.Connection) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    rows = c.execute("select * from guild_hashes order by guild_id, hash_id").fetchall()
    fks = c.execute("pragma foreign_key_list(guild_hashes)").fetchall()
    return rows, fks


def test_upgrade_rekeys_per_server_and_keeps_every_row(tmp_path: Path) -> None:
    db = tmp_path / "optimus.db"
    cfg = _cfg(db)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        command.upgrade(cfg, "0010")
        with sqlite3.connect(db) as c:
            c.execute("insert into guilds(guild_id) values (1), (2)")
            c.execute(
                "insert into guild_hashes"
                "(guild_id, hash_id, phash, dhash, whash, ahash, source, added_by) values"
                " (1, '00000000000000aa', 170, 2, 3, 9, 'import', 77),"
                " (1, '00000000000000bb', 187, 2, 3, 0, 'local', null),"
                " (2, '00000000000000cc', 204, 2, 3, 0, 'local', null)"
            )
            assert _pk(c) == ["hash_id"]
            before = _snapshot(c)

        command.upgrade(cfg, "0011")
        with sqlite3.connect(db) as c:
            assert _pk(c) == ["guild_id", "hash_id"]
            assert _snapshot(c) == before  # every row and column; FK with CASCADE
            assert any(fk[6] == "CASCADE" for fk in before[1])
            # The bug: the same image on a second server now stores.
            c.execute(
                "insert into guild_hashes(guild_id, hash_id, phash, dhash, whash)"
                " values (2, '00000000000000aa', 170, 2, 3)"
            )
            # ...and one server still cannot list it twice.
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "insert into guild_hashes(guild_id, hash_id, phash, dhash, whash)"
                    " values (1, '00000000000000aa', 170, 2, 3)"
                )
            c.rollback()

        command.downgrade(cfg, "0010")
        with sqlite3.connect(db) as c:
            assert _pk(c) == ["hash_id"]
            assert _snapshot(c) == before
