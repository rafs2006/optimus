"""Tests for hot-path guild config: DB load with related rows and Redis caching."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import fakeredis.aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.core.config import Sensitivity
from optimus.core.guild_config import GuildConfig, GuildConfigCache, load_from_db
from optimus.db.models import (
    Guild,
    GuildChannelIgnored,
    GuildRoleIgnored,
    GuildTrustedUser,
    UserOptout,
)


async def test_load_from_db_unconfigured_guild_returns_default(session: AsyncSession) -> None:
    config = await load_from_db(session, 12345)
    assert config == GuildConfig.default(12345)
    assert config.sensitivity is Sensitivity.BALANCED
    assert config.ignored_channels == frozenset()


async def test_load_from_db_hydrates_related_rows(session: AsyncSession) -> None:
    session.add(
        Guild(
            guild_id=1,
            sensitivity="strict",
            optin_scan_bots=True,
            safe_mode=True,
        )
    )
    session.add_all(
        [
            GuildChannelIgnored(guild_id=1, channel_id=100),
            GuildChannelIgnored(guild_id=1, channel_id=101),
            GuildRoleIgnored(guild_id=1, role_id=200),
            GuildTrustedUser(guild_id=1, user_id=300),
        ]
    )
    await session.flush()

    config = await load_from_db(session, 1)
    assert config.sensitivity is Sensitivity.STRICT
    assert config.scan_bots is True
    assert config.safe_mode is True
    assert config.ignored_channels == frozenset({100, 101})
    assert config.ignored_roles == frozenset({200})
    assert config.trusted_users == frozenset({300})


def test_guild_config_json_roundtrip() -> None:
    original = GuildConfig(
        guild_id=7,
        sensitivity=Sensitivity.PERMISSIVE,
        scan_bots=True,
        safe_mode=False,
        ignored_channels=frozenset({1, 2}),
        ignored_roles=frozenset({3}),
        trusted_users=frozenset({4, 5}),
    )
    assert GuildConfig.from_json(original.to_json()) == original


@asynccontextmanager
async def _loader(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield session


async def test_cache_populates_on_miss_and_serves_on_hit(session: AsyncSession) -> None:
    session.add(Guild(guild_id=5, sensitivity="strict", optin_scan_bots=True))
    await session.flush()

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = GuildConfigCache(redis, lambda: _loader(session), ttl_seconds=120)

    # Miss: loads from DB and writes through to Redis with the TTL.
    first = await cache.get(5)
    assert first.sensitivity is Sensitivity.STRICT
    assert await redis.get("optimus:guildcfg:5") is not None
    assert 0 < await redis.ttl("optimus:guildcfg:5") <= 120

    # Hit: served from Redis even after the underlying row changes.
    guild = await session.get(Guild, 5)
    assert guild is not None
    guild.sensitivity = "permissive"
    await session.flush()
    second = await cache.get(5)
    assert second.sensitivity is Sensitivity.STRICT  # stale cache wins until invalidated
    await redis.aclose()


async def test_cache_invalidate_forces_reload(session: AsyncSession) -> None:
    session.add(Guild(guild_id=8, sensitivity="strict"))
    await session.flush()

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = GuildConfigCache(redis, lambda: _loader(session))

    await cache.get(8)
    guild = await session.get(Guild, 8)
    assert guild is not None
    guild.sensitivity = "permissive"
    await session.flush()

    await cache.invalidate(8)
    assert await redis.get("optimus:guildcfg:8") is None
    reloaded = await cache.get(8)
    assert reloaded.sensitivity is Sensitivity.PERMISSIVE
    await redis.aclose()


async def test_cache_without_redis_always_hits_db(session: AsyncSession) -> None:
    session.add(Guild(guild_id=9, sensitivity="permissive"))
    await session.flush()
    cache = GuildConfigCache(None, lambda: _loader(session))
    config = await cache.get(9)
    assert config.sensitivity is Sensitivity.PERMISSIVE
    # invalidate is a no-op when there is no cache backend.
    await cache.invalidate(9)


def _scan(config: GuildConfig, uploader_id: int) -> bool:
    return config.should_scan(
        channel_id=1,
        uploader_id=uploader_id,
        author_role_ids=frozenset(),
        is_bot=False,
        is_webhook=False,
    )


def test_opted_out_uploader_is_never_scanned() -> None:
    config = GuildConfig(guild_id=1, opted_out_users=frozenset({7}))
    assert _scan(config, 7) is False
    assert _scan(config, 8) is True


async def test_load_from_db_carries_opt_outs_into_the_scan_policy(
    session: AsyncSession,
) -> None:
    session.add(Guild(guild_id=20, sensitivity="balanced"))
    session.add(UserOptout(user_id=7))
    await session.flush()

    config = await load_from_db(session, 20)
    assert config.opted_out_users == frozenset({7})
    assert _scan(config, 7) is False


async def test_opt_out_applies_in_a_guild_that_never_ran_setup(session: AsyncSession) -> None:
    # The unconfigured-guild shortcut must not skip the opt-out lookup: an
    # erasure request is global and does not depend on a Guild row existing.
    session.add(UserOptout(user_id=7))
    await session.flush()

    config = await load_from_db(session, 999)
    assert _scan(config, 7) is False


def test_opt_outs_survive_a_cache_round_trip() -> None:
    config = GuildConfig(guild_id=1, opted_out_users=frozenset({7, 9}))
    assert GuildConfig.from_json(config.to_json()) == config


def test_snapshot_cached_by_an_older_build_still_loads() -> None:
    # A JSON blob written before this field existed must not fail every lookup
    # until its TTL expires.
    legacy = (
        '{"guild_id":1,"sensitivity":"balanced","scan_bots":false,"safe_mode":false,'
        '"ignored_channels":[],"ignored_roles":[],"trusted_users":[]}'
    )
    assert GuildConfig.from_json(legacy).opted_out_users == frozenset()
