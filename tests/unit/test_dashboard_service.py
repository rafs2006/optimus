"""Handler tests for the dashboard: login flow, gating, and page rendering.

Runs the real aiohttp routes against an in-memory database and a fake OAuth
client, exercising the same paths a browser would hit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.dashboard.auth import PERM_MANAGE_GUILD, OAuthGuild, OAuthUser
from optimus.dashboard.service import DashboardService
from optimus.db.engine import create_engine, create_session_factory, session_scope
from optimus.db.models import Base, Detection, Guild, ModAction

SECRET = "service-test-secret-key-0123456789abcdef-0123456789"
OWNER_ID = 999
MOD_ID = 7

Scope = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: The OAuth guild list for the standard moderator persona: a mod in guild 101,
#: a plain member of bot guild 202, and an admin of 303 which the bot is NOT in.
MOD_GUILDS = [
    OAuthGuild(id=101, name="Alpha", permissions=PERM_MANAGE_GUILD),
    OAuthGuild(id=202, name="Beta", permissions=0),
    OAuthGuild(id=303, name="Elsewhere", permissions=PERM_MANAGE_GUILD),
]


class FakeOAuth:
    """In-memory OAuthClient: any code exchanges into the configured user."""

    def __init__(self, user: OAuthUser, guilds: list[OAuthGuild]) -> None:
        self._user = user
        self._guilds = guilds
        self.closed = False

    def authorize_url(self, *, state: str) -> str:
        return f"https://discord.test/oauth2/authorize?state={state}"

    async def exchange_code(self, code: str) -> str:
        return f"token-for-{code}"

    async def fetch_user(self, access_token: str) -> OAuthUser:
        return self._user

    async def fetch_guilds(self, access_token: str) -> list[OAuthGuild]:
        return list(self._guilds)

    async def close(self) -> None:
        self.closed = True


async def _owner_ids() -> set[int]:
    return {OWNER_ID}


@dataclass
class Env:
    scope: Scope


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    """In-memory schema with the bot installed in guilds 101 and 202."""
    engine = create_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)

    def scope() -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(factory)

    async with scope() as s:
        s.add_all([Guild(guild_id=101), Guild(guild_id=202)])
        await s.commit()
    yield Env(scope=scope)
    await engine.dispose()


@asynccontextmanager
async def client_for(
    env: Env, *, user: OAuthUser, guilds: list[OAuthGuild]
) -> AsyncIterator[TestClient]:
    service = DashboardService(
        secret=SECRET,
        session_ttl_seconds=3600.0,
        secure_cookies=False,
        oauth=FakeOAuth(user, guilds),
        scope=env.scope,
        fetch_owner_ids=_owner_ids,
    )
    app = web.Application()
    app.add_routes(service.routes())
    async with TestClient(TestServer(app)) as client:
        yield client


async def _login(client: TestClient) -> web.Response:
    """Drive the OAuth round-trip: /dash/login → callback with the state."""
    resp = await client.get("/dash/login", allow_redirects=False)
    assert resp.status == 302
    state = parse_qs(urlparse(resp.headers["Location"]).query)["state"][0]
    out = await client.get(f"/dash/callback?code=abc&state={state}", allow_redirects=False)
    return out  # type: ignore[return-value]


async def _seed_detection(env: Env, guild_id: int, key: str) -> int:
    async with env.scope() as s:
        row = Detection(
            guild_id=guild_id,
            message_id=1,
            channel_id=2,
            attachment_id=3,
            uploader_id=4,
            distances={"phash": 5},
            hashes={"phash": "00ff00ff00ff00ff"},
            verdict="scam",
            action_taken="delete",
            idempotency_key=key,
        )
        s.add(row)
        await s.commit()
        return row.id


class TestLoginFlow:
    async def test_anonymous_home_offers_login(self, env: Env) -> None:
        async with client_for(env, user=OAuthUser(MOD_ID, "mod"), guilds=MOD_GUILDS) as c:
            resp = await c.get("/dash")
            text = await resp.text()
            assert resp.status == 200
            assert "/dash/login" in text

    async def test_protected_page_redirects_anonymous(self, env: Env) -> None:
        async with client_for(env, user=OAuthUser(MOD_ID, "mod"), guilds=MOD_GUILDS) as c:
            resp = await c.get("/dash/guild/101", allow_redirects=False)
            assert resp.status == 302
            assert resp.headers["Location"] == "/dash"

    async def test_login_lists_only_moderated_bot_guilds(self, env: Env) -> None:
        async with client_for(env, user=OAuthUser(MOD_ID, "mod"), guilds=MOD_GUILDS) as c:
            resp = await _login(c)
            assert resp.status == 302
            home = await (await c.get("/dash")).text()
            assert "Alpha" in home  # mod in a bot guild
            assert "Beta" not in home  # plain member: hidden
            assert "Elsewhere" not in home  # bot not installed there
            assert "Owner" not in home  # not the deployment owner

    async def test_callback_rejects_state_mismatch(self, env: Env) -> None:
        async with client_for(env, user=OAuthUser(MOD_ID, "mod"), guilds=MOD_GUILDS) as c:
            resp = await c.get("/dash/login", allow_redirects=False)
            assert resp.status == 302
            resp = await c.get("/dash/callback?code=abc&state=wrong", allow_redirects=False)
            assert resp.status == 400

    async def test_callback_without_any_access_is_403(self, env: Env) -> None:
        async with client_for(env, user=OAuthUser(5, "randem"), guilds=[]) as c:
            resp = await _login(c)
            assert resp.status == 403

    async def test_logout_clears_session(self, env: Env) -> None:
        async with client_for(env, user=OAuthUser(MOD_ID, "mod"), guilds=MOD_GUILDS) as c:
            await _login(c)
            await c.get("/dash/logout", allow_redirects=False)
            resp = await c.get("/dash/guild/101", allow_redirects=False)
            assert resp.status == 302


class TestGating:
    async def test_mod_can_view_own_guild_only(self, env: Env) -> None:
        async with client_for(env, user=OAuthUser(MOD_ID, "mod"), guilds=MOD_GUILDS) as c:
            await _login(c)
            assert (await c.get("/dash/guild/101")).status == 200
            assert (await c.get("/dash/guild/202")).status == 403
            assert (await c.get("/dash/guild/101/audit")).status == 200

    async def test_global_pages_are_owner_only(self, env: Env) -> None:
        async with client_for(env, user=OAuthUser(MOD_ID, "mod"), guilds=MOD_GUILDS) as c:
            await _login(c)
            for path in ("/dash/global", "/dash/global/hashes", "/dash/global/servers"):
                assert (await c.get(path)).status == 403

    async def test_owner_sees_all_guilds_and_global(self, env: Env) -> None:
        async with client_for(env, user=OAuthUser(OWNER_ID, "owner"), guilds=[]) as c:
            resp = await _login(c)
            assert resp.status == 302  # owner logs in fine with zero mod guilds
            assert (await c.get("/dash/guild/202")).status == 200
            for path in ("/dash/global", "/dash/global/hashes", "/dash/global/servers"):
                assert (await c.get(path)).status == 200


class TestPages:
    async def test_guild_page_renders_detections(self, env: Env) -> None:
        await _seed_detection(env, 101, "k1")
        async with client_for(env, user=OAuthUser(MOD_ID, "mod"), guilds=MOD_GUILDS) as c:
            await _login(c)
            text = await (await c.get("/dash/guild/101")).text()
            assert "scam" in text
            assert "Scan activity" in text

    async def test_detection_detail_and_cross_guild_isolation(self, env: Env) -> None:
        own = await _seed_detection(env, 101, "own")
        foreign = await _seed_detection(env, 202, "foreign")
        async with client_for(env, user=OAuthUser(MOD_ID, "mod"), guilds=MOD_GUILDS) as c:
            await _login(c)
            detail = await c.get(f"/dash/guild/101/detection/{own}")
            assert detail.status == 200
            text = await detail.text()
            assert "00ff00ff00ff00ff" in text
            assert "delete" in text
            assert (await c.get(f"/dash/guild/101/detection/{foreign}")).status == 404

    async def test_audit_page_lists_actions(self, env: Env) -> None:
        async with env.scope() as s:
            s.add(ModAction(guild_id=101, actor_id=1, action="review.confirm_scam", payload={}))
            await s.commit()
        async with client_for(env, user=OAuthUser(MOD_ID, "mod"), guilds=MOD_GUILDS) as c:
            await _login(c)
            text = await (await c.get("/dash/guild/101/audit")).text()
            assert "review.confirm_scam" in text

    async def test_global_hash_queue_renders(self, env: Env) -> None:
        async with client_for(env, user=OAuthUser(OWNER_ID, "owner"), guilds=[]) as c:
            await _login(c)
            text = await (await c.get("/dash/global/hashes?status=promoted")).text()
            assert "No promoted hashes" in text
