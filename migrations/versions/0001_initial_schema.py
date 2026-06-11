"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guilds",
        sa.Column("guild_id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("sensitivity", sa.String(length=16), nullable=False, server_default="balanced"),
        sa.Column(
            "action_policy", sa.String(length=32), nullable=False, server_default="report_only"
        ),
        sa.Column("mod_queue_threshold", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("review_channel_id", sa.BigInteger(), nullable=True),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("optin_global_db", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("optin_scan_bots", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "optin_evidence_storage", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("locale", sa.String(length=8), nullable=False, server_default="en"),
        sa.Column("safe_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_guilds_tenant_id", "guilds", ["tenant_id"])

    op.create_table(
        "guild_channels_ignored",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("channel_id", sa.BigInteger(), primary_key=True),
    )
    op.create_index("ix_guild_channels_ignored_guild_id", "guild_channels_ignored", ["guild_id"])

    op.create_table(
        "guild_roles_ignored",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role_id", sa.BigInteger(), primary_key=True),
    )
    op.create_index("ix_guild_roles_ignored_guild_id", "guild_roles_ignored", ["guild_id"])

    op.create_table(
        "guild_trusted_users",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
    )
    op.create_index("ix_guild_trusted_users_guild_id", "guild_trusted_users", ["guild_id"])

    op.create_table(
        "guild_hashes",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("hash_id", sa.String(length=64), primary_key=True),
        sa.Column("phash", sa.BigInteger(), nullable=False),
        sa.Column("dhash", sa.BigInteger(), nullable=False),
        sa.Column("whash", sa.BigInteger(), nullable=False),
        sa.Column("ahash", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_guild_hashes_guild_id", "guild_hashes", ["guild_id"])

    op.create_table(
        "guild_whitelist",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phash", sa.BigInteger(), nullable=False),
        sa.Column("dhash", sa.BigInteger(), nullable=False),
        sa.Column("whash", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_guild_whitelist_guild_id", "guild_whitelist", ["guild_id"])

    op.create_table(
        "global_hashes",
        sa.Column("hash_id", sa.String(length=64), primary_key=True),
        sa.Column("phash", sa.BigInteger(), nullable=False),
        sa.Column("dhash", sa.BigInteger(), nullable=False),
        sa.Column("whash", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="candidate"),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_global_hashes_status", "global_hashes", ["status"])
    op.create_index("ix_global_hashes_campaign_id", "global_hashes", ["campaign_id"])

    op.create_table(
        "global_hash_approvals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "hash_id",
            sa.String(length=64),
            sa.ForeignKey("global_hashes.hash_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("approver_user_id", sa.BigInteger(), nullable=False),
        sa.Column("approver_guild_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("hash_id", "approver_user_id", name="uq_global_approval"),
    )
    op.create_index("ix_global_hash_approvals_hash_id", "global_hash_approvals", ["hash_id"])

    op.create_table(
        "detections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("uploader_id", sa.BigInteger(), nullable=False),
        sa.Column("distances", sa.JSON(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("action_taken", sa.String(length=32), nullable=False, server_default="none"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_detection_idempotency"),
    )
    op.create_index("ix_detections_guild_id", "detections", ["guild_id"])

    op.create_table(
        "appeals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "detection_id",
            sa.Integer(),
            sa.ForeignKey("detections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("resolved_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_appeals_guild_id", "appeals", ["guild_id"])

    op.create_table(
        "mod_actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_mod_actions_guild_id", "mod_actions", ["guild_id"])

    op.create_table(
        "users_optout",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "stats_rollups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("false_positives", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.UniqueConstraint("guild_id", "bucket_start", name="uq_stats_bucket"),
    )
    op.create_index("ix_stats_rollups_guild_id", "stats_rollups", ["guild_id"])

    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "detection_id",
            sa.Integer(),
            sa.ForeignKey("detections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("object_key", sa.String(length=256), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evidence_detection_id", "evidence", ["detection_id"])


def downgrade() -> None:
    for table in (
        "evidence",
        "stats_rollups",
        "users_optout",
        "mod_actions",
        "appeals",
        "detections",
        "global_hash_approvals",
        "global_hashes",
        "guild_whitelist",
        "guild_hashes",
        "guild_trusted_users",
        "guild_roles_ignored",
        "guild_channels_ignored",
        "guilds",
    ):
        op.drop_table(table)
