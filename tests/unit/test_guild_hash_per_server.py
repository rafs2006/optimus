"""Two servers can list the same scam image; one server never lists it twice.

``hash_id`` is derived from the image (``f"{phash:016x}"``), so the same scam
gets the same id everywhere. While ``hash_id`` alone was the table's primary
key, the first server to store an image owned that id for the deployment, and
every later attempt -- ``/scamhash import`` of a shared export, ``/scamhash
add``, Confirm scam -- crashed with ``UNIQUE constraint failed:
guild_hashes.hash_id`` ("This interaction is no longer valid." in Discord).
Re-adding within one server crashed the same way.

These run the real ``DbDeps`` against real SQLite: the in-memory fakes key
hashes by id per test and could never have shown either collision.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.core.config import get_settings
from optimus.core.ratelimit import RateLimit
from optimus.db.models import GuildHash
from optimus.db.repositories import GuildHashRepository, GuildRepository
from optimus.services.interactions.handlers import InteractionContext, handle_command
from optimus.services.interactions.logic import ImportHash, build_export
from optimus.services.interactions.service import DbDeps

SMALL = 111111111111111111
MAIN = 333333333333333333
MOD = 444444444444444444
MANAGE_GUILD = 1 << 5


class _NoopRateLimiter:
    async def acquire(self, key: str, limit: RateLimit, cost: float = 1.0) -> bool:
        return True


def _deps(session: AsyncSession) -> DbDeps:
    return DbDeps(session, _NoopRateLimiter(), get_settings())  # type: ignore[arg-type]


def _gh(
    phash: int, *, guild_id: int | None = None, source: str = "local", added_by: int | None = None
) -> GuildHash:
    row = GuildHash(
        hash_id=f"{phash:016x}",
        phash=phash,
        dhash=phash ^ 1,
        whash=phash ^ 2,
        ahash=0,
        source=source,
        added_by=added_by,
    )
    if guild_id is not None:
        row.guild_id = guild_id
    return row


async def _servers(session: AsyncSession) -> None:
    for gid in (SMALL, MAIN):
        await GuildRepository(session).get_or_create(gid)


def _ctx(guild_id: int, subcommand: str, **options: object) -> InteractionContext:
    return InteractionContext(
        guild_id=guild_id,
        user_id=MOD,
        member_permissions=MANAGE_GUILD,
        command="scamhash",
        subcommand=subcommand,
        options=dict(options),
    )


async def _rows(session: AsyncSession, guild_id: int) -> list[GuildHash]:
    return list(await GuildHashRepository(session, guild_id).list_active())


async def test_two_servers_can_store_the_same_image(session: AsyncSession) -> None:
    await _servers(session)
    deps = _deps(session)

    await deps.add_guild_hash(SMALL, _gh(0xAA))
    await deps.add_guild_hash(MAIN, _gh(0xAA))
    await session.commit()

    assert [r.hash_id for r in await _rows(session, SMALL)] == ["00000000000000aa"]
    assert [r.hash_id for r in await _rows(session, MAIN)] == ["00000000000000aa"]


async def test_readding_on_the_same_server_keeps_the_original_row(
    session: AsyncSession,
) -> None:
    """/scamhash add twice, or Confirm scam on a listed image: no crash, no overwrite."""
    await _servers(session)
    deps = _deps(session)

    first = await deps.add_guild_hash(MAIN, _gh(0xAA, source="local", added_by=1))
    again = await deps.add_guild_hash(MAIN, _gh(0xAA, source="reviewmsg", added_by=2))
    await session.commit()

    assert again is first
    (row,) = await _rows(session, MAIN)
    assert (row.source, row.added_by) == ("local", 1)


async def test_export_from_one_server_imports_into_another_that_shares_hashes(
    session: AsyncSession,
) -> None:
    """The reported case: the main server already had some of the small one's images."""
    await _servers(session)
    deps = _deps(session)
    for phash in (0xAA, 0xBB, 0xCC):
        await deps.add_guild_hash(SMALL, _gh(phash))
    await deps.add_guild_hash(MAIN, _gh(0xBB))  # already known on the main server
    await session.commit()

    export = await handle_command(_ctx(SMALL, "export"), deps)
    assert export.i18n_key == "command.export_ok"
    assert isinstance(export.attachment, str)

    resp = await handle_command(_ctx(MAIN, "import", file=export.attachment.encode()), deps)
    await session.commit()

    assert resp.i18n_key == "command.import_ok"
    assert resp.params == {"added": 2, "skipped": 1}
    assert sorted(r.phash for r in await _rows(session, MAIN)) == [0xAA, 0xBB, 0xCC]
    # The source server is untouched.
    assert sorted(r.phash for r in await _rows(session, SMALL)) == [0xAA, 0xBB, 0xCC]


async def test_reimporting_the_same_file_adds_nothing_and_does_not_crash(
    session: AsyncSession,
) -> None:
    await _servers(session)
    deps = _deps(session)
    body = build_export([ImportHash(phash=p, dhash=p + 1, whash=p + 2) for p in (1, 2, 3)])

    first = await handle_command(_ctx(MAIN, "import", file=body), deps)
    await session.commit()
    second = await handle_command(_ctx(MAIN, "import", file=body), deps)
    await session.commit()

    assert first.params == {"added": 3, "skipped": 0}
    assert second.params == {"added": 0, "skipped": 3}
    assert len(await _rows(session, MAIN)) == 3


async def test_removing_on_one_server_leaves_the_other_alone(session: AsyncSession) -> None:
    await _servers(session)
    deps = _deps(session)
    await deps.add_guild_hash(SMALL, _gh(0xAA))
    await deps.add_guild_hash(MAIN, _gh(0xAA))
    await session.commit()

    assert await deps.remove_guild_hash(MAIN, "00000000000000aa") == 1
    await session.commit()

    assert await _rows(session, MAIN) == []
    assert [r.hash_id for r in await _rows(session, SMALL)] == ["00000000000000aa"]


async def test_the_key_is_per_server(session: AsyncSession) -> None:
    """Pin the schema itself, so a future model edit cannot quietly revert it."""
    assert [c.name for c in GuildHash.__table__.primary_key.columns] == ["guild_id", "hash_id"]
    await _servers(session)
    session.add_all([_gh(7, guild_id=SMALL), _gh(7, guild_id=MAIN)])
    await session.commit()
    count = len((await session.execute(select(GuildHash))).scalars().all())
    assert count == 2


@pytest.mark.parametrize("phash", [0, 2**64 - 1])
async def test_extreme_hash_values_store_on_two_servers(session: AsyncSession, phash: int) -> None:
    await _servers(session)
    deps = _deps(session)
    await deps.add_guild_hash(SMALL, _gh(phash))
    await deps.add_guild_hash(MAIN, _gh(phash))
    await session.commit()
    assert len(await _rows(session, SMALL)) == len(await _rows(session, MAIN)) == 1
