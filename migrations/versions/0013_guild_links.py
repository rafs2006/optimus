"""Linked servers: groups of servers that keep one blocklist.

``guild_links`` maps a server to its group. A server is in at most one group
(the primary key). New table only, no change to existing rows: safe on a live
SQLite volume.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guild_links",
        sa.Column("guild_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("group_id", sa.String(length=64), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_guild_links_group_id", "guild_links", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_guild_links_group_id", table_name="guild_links")
    op.drop_table("guild_links")
