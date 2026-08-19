"""Store the flagged image's hash ensemble on the detection row.

The review-card buttons (*Confirm scam*, *False positive*, *Whitelist image*,
*Submit to global*) act on the image behind a detection long after the
original bytes are gone -- Discord CDN URLs expire and the message may be
deleted. Persisting the perceptual hashes the detection worker already
computed makes those buttons self-sufficient: confirming adds the hashes to
the guild blocklist, whitelisting adds them to the whitelist, with no
re-fetch needed. Nullable because member reports deliberately skip hashing
(a hostile reporter must not be able to poison anything) and rows written
before this migration have no hashes to backfill.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("detections", sa.Column("hashes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("detections", "hashes")
