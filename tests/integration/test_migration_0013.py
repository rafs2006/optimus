"""Migration 0013 adds the linked-servers table and removes it on downgrade."""

from __future__ import annotations

import sqlite3
import warnings
from pathlib import Path

from alembic import command

from tests.integration.test_migration_0012 import _cfg


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as c:
        return {r[0] for r in c.execute("select name from sqlite_master where type='table'")}


def test_upgrade_and_downgrade(tmp_path: Path) -> None:
    db = tmp_path / "optimus.db"
    cfg = _cfg(db)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        command.upgrade(cfg, "0012")
        assert "guild_links" not in _tables(db)
        command.upgrade(cfg, "0013")
    assert "guild_links" in _tables(db)
    with sqlite3.connect(db) as c:
        cols = [r[1] for r in c.execute("pragma table_info(guild_links)")]
        idx = {r[1] for r in c.execute("pragma index_list(guild_links)")}
    assert cols == ["guild_id", "group_id", "added_by", "created_at"]
    assert "ix_guild_links_group_id" in idx
    command.downgrade(cfg, "0012")
    assert "guild_links" not in _tables(db)
