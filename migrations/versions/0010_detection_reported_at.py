"""Track when a detection's moderator review card was posted.

The join backfill runs before an admin has linked a review channel with
``/setup``, and any detection it produces was silently dropped at the report
boundary (``review_channel_id`` was still ``None``). The detection row itself
was persisted -- only the card delivery failed -- so the evidence is there,
we just could not tell after the fact which rows had actually been surfaced.

``reported_at`` closes that gap. ``NULL`` means the card was never posted:
those are the rows the ``/setup`` backlog replay finds. A stamped row is
never replayed again, which is what keeps replay idempotent under bus
redelivery and re-invocations of ``/setup``.

The paired composite index ``(guild_id, reported_at, created_at)`` scopes the
replay's ``WHERE guild_id = ? AND reported_at IS NULL AND created_at >= ?
ORDER BY created_at DESC LIMIT 50`` to a single index range read.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable additive column: existing rows get NULL ("card never posted"),
    # which is the correct default for the backlog replay. No backfill or
    # rewrite; safe on a live SQLite volume with the bot running.
    op.add_column(
        "detections",
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_detections_guild_reported",
        "detections",
        ["guild_id", "reported_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_detections_guild_reported", table_name="detections")
    op.drop_column("detections", "reported_at")
