"""Remember which review card each detection was reported on.

One message with several flagged images used to post one card per image.
Detections from the same message now share a single card, and the buttons on
it act on every image at once, so each row needs to know which card it lives
on. ``review_message_id`` is the review-channel message id of that card;
``NULL`` means no card yet, or a card posted before this migration (those keep
the old one-card-per-image behaviour).

The ``(guild_id, review_message_id)`` index serves the button lookup "every
detection on this card", which runs on every click.

Nullable additive column with no backfill: safe on a live SQLite volume.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "detections",
        sa.Column("review_message_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_detections_guild_card",
        "detections",
        ["guild_id", "review_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_detections_guild_card", table_name="detections")
    op.drop_column("detections", "review_message_id")
