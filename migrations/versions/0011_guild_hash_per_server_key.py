"""Key guild_hashes by (guild_id, hash_id) instead of hash_id alone.

``hash_id`` is derived from the image's perceptual hash (``f"{phash:016x}"``),
so the same scam image gets the same id on every server. With ``hash_id`` as
the table's sole primary key, the first server to store an image owned that id
for the whole deployment: any other server adding the same image -- by
``/scamhash import`` from a shared export, ``/scamhash add``, a review card or
the campaign sweep -- failed with ``UNIQUE constraint failed:
guild_hashes.hash_id``. Every read already filters by ``guild_id``, so the id
only ever needed to be unique within a server.

Existing rows keep their ids, so ``/scamhash list`` / ``remove`` are
unchanged. On SQLite the primary key cannot be altered in place, so batch mode
rebuilds the table and copies every row; on Postgres the constraint is swapped
in place. Downgrade restores the single-column key and fails loudly if two
servers have since stored the same image, rather than discarding one.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import warnings

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None

_PK = "pk_guild_hashes"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _current_pk_name() -> str | None:
    return sa.inspect(op.get_bind()).get_pk_constraint("guild_hashes").get("name")


def _rebuild_sqlite(key: list[str]) -> None:
    """Rebuild the table with primary key ``key``, copying every row.

    SQLite cannot alter a primary key, so batch mode recreates the table from
    its reflected definition -- every column, index and the guilds foreign key
    (with its ON DELETE CASCADE) carried over -- with the new key swapped in.
    SQLAlchemy warns while the reflected copy's column flags still name the old
    key; batch mode then applies the new one, which the migration test pins.
    The warning is silenced here only, for that one message.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=r".*specifies columns .* as primary_key=True, not matching"
        )
        with op.batch_alter_table("guild_hashes", recreate="always") as batch:
            batch.create_primary_key(_PK, key)


def upgrade() -> None:
    if _is_postgres():
        old = _current_pk_name() or "guild_hashes_pkey"
        op.drop_constraint(old, "guild_hashes", type_="primary")
        op.create_primary_key(_PK, "guild_hashes", ["guild_id", "hash_id"])
        return
    _rebuild_sqlite(["guild_id", "hash_id"])


def downgrade() -> None:
    if _is_postgres():
        op.drop_constraint(_PK, "guild_hashes", type_="primary")
        op.create_primary_key("guild_hashes_pkey", "guild_hashes", ["hash_id"])
        return
    _rebuild_sqlite(["hash_id"])
