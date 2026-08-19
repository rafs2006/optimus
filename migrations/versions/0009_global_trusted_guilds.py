"""Contribution allowlist for the shared global hash database.

Promotion votes (Confirm-scam clicks) only count from servers the bot owner
has explicitly approved -- this is the Sybil/collusion defense for the global
set: a hostile actor spinning up throwaway servers gets zero votes. Any guild
may still opt in to *consume* promoted hashes; contribution is the privileged
direction. Managed at runtime via the owner-only ``/global`` command.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "global_trusted_guilds",
        sa.Column("guild_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("added_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("global_trusted_guilds")
