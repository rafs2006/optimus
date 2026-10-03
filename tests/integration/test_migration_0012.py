"""Migration 0012 adds the detection -> review card link without touching rows.

Runs against a real SQLite file, as production does: existing detections keep
every column and get ``NULL`` (no linked card, so their old cards keep acting
on one image), the lookup index exists, and the downgrade removes both.
"""

from __future__ import annotations

import sqlite3
import warnings
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]


def _cfg(db: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    return cfg


def _columns(c: sqlite3.Connection) -> list[str]:
    return [r[1] for r in c.execute("pragma table_info(detections)")]


def _indexes(c: sqlite3.Connection) -> set[str]:
    return {r[1] for r in c.execute("pragma index_list(detections)")}


def test_upgrade_adds_the_card_link_and_keeps_rows(tmp_path: Path) -> None:
    db = tmp_path / "optimus.db"
    cfg = _cfg(db)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        command.upgrade(cfg, "0011")
        with sqlite3.connect(db) as c:
            c.execute(
                "insert into detections(guild_id, message_id, channel_id, attachment_id,"
                " uploader_id, distances, verdict, action_taken, idempotency_key)"
                " values (1, 2, 3, 4, 5, '{}', 'scam', 'none', 'k1')"
            )
            before = c.execute("select * from detections").fetchall()

        command.upgrade(cfg, "0012")
        with sqlite3.connect(db) as c:
            assert "review_message_id" in _columns(c)
            assert "ix_detections_guild_card" in _indexes(c)
            (row,) = c.execute("select * from detections").fetchall()
            assert row[: len(before[0])] == before[0]
            assert row[-1] is None

        command.downgrade(cfg, "0011")
        with sqlite3.connect(db) as c:
            assert "review_message_id" not in _columns(c)
            assert "ix_detections_guild_card" not in _indexes(c)
            assert c.execute("select * from detections").fetchall() == before
