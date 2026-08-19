"""The dashboard's aiohttp routes: login flow, guild pages, owner pages.

Mounted onto the existing :class:`~optimus.core.health.HealthServer` app so
simple-mode deployments (one process, one port — e.g. Railway) get the
dashboard on the same public URL they already expose for health checks.

Access model
    * A **guild moderator** (MANAGE_GUILD or ADMINISTRATOR in a guild the bot
      is in) can view that guild's pages: scan activity, detections, audit log.
    * The **deployment owner** (Discord application owner or team member) can
      additionally view every guild page and the global sections: cross-guild
      overview, the global hash queue, and the trusted-server list.

Phase 1 is read-only: there is deliberately no route that mutates anything.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable

from aiohttp import web

from optimus.core.config import Settings
from optimus.core.logging import get_logger
from optimus.dashboard import queries, render
from optimus.dashboard.auth import (
    DashboardSession,
    DiscordOAuthClient,
    OAuthClient,
    can_moderate,
    sign_payload,
    verify_payload,
)
from optimus.db.engine import SessionScope
from optimus.db.repositories import GuildListRepository

_log = get_logger(__name__)

SESSION_COOKIE = "optimus_dash"
STATE_COOKIE = "optimus_dash_state"
_STATE_TTL_SECONDS = 600.0
_HASH_STATUSES = ("candidate", "promoted", "revoked")


class DashboardService:
    """Builds and serves all ``/dash`` routes against the bot's database."""

    def __init__(
        self,
        *,
        secret: str,
        session_ttl_seconds: float,
        secure_cookies: bool,
        oauth: OAuthClient,
        scope: SessionScope,
        fetch_owner_ids: Callable[[], Awaitable[set[int]]],
    ) -> None:
        self._secret = secret
        self._session_ttl = session_ttl_seconds
        self._secure = secure_cookies
        self._oauth = oauth
        self._scope = scope
        self._fetch_owner_ids = fetch_owner_ids

    # --- Mounting -----------------------------------------------------------------

    def routes(self) -> list[web.RouteDef]:
        """All dashboard routes, ready to add to an aiohttp application."""
        return [
            web.get("/dash", self.handle_home),
            web.get("/dash/login", self.handle_login),
            web.get("/dash/callback", self.handle_callback),
            web.get("/dash/logout", self.handle_logout),
            web.get("/dash/guild/{guild_id}", self.handle_guild),
            web.get("/dash/guild/{guild_id}/detection/{detection_id}", self.handle_detection),
            web.get("/dash/guild/{guild_id}/audit", self.handle_audit),
            web.get("/dash/global", self.handle_global),
            web.get("/dash/global/hashes", self.handle_global_hashes),
            web.get("/dash/global/servers", self.handle_global_servers),
        ]

    async def close(self) -> None:
        """Release the OAuth client's HTTP resources (app cleanup hook)."""
        await self._oauth.close()

    # --- Session plumbing ----------------------------------------------------------

    def _session_from(self, request: web.Request) -> DashboardSession | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        return DashboardSession.from_token(token, secret=self._secret)

    def _require_session(self, request: web.Request) -> DashboardSession:
        session = self._session_from(request)
        if session is None:
            raise web.HTTPFound("/dash")
        return session

    def _set_cookie(
        self, response: web.StreamResponse, name: str, value: str, *, max_age: int
    ) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path="/dash",
            httponly=True,
            secure=self._secure,
            samesite="Lax",
        )

    @staticmethod
    def _html(body: str, *, status: int = 200) -> web.Response:
        return web.Response(text=body, content_type="text/html", status=status)

    def _forbidden(self, session: DashboardSession, message: str) -> web.Response:
        body = render.page(
            "Not authorized",
            f'<p class="muted">{render.esc(message)}</p><p><a href="/dash">Back</a></p>',
            user=session.username,
        )
        return self._html(body, status=403)

    # --- Login flow -----------------------------------------------------------------

    async def handle_home(self, request: web.Request) -> web.Response:
        """Landing page: login prompt, or the visitor's guild list."""
        session = self._session_from(request)
        if session is None:
            body = (
                '<p class="muted">Read-only moderation dashboard for the Optimus '
                "anti-scam bot. Log in with Discord; you will see the servers where "
                "you have Manage Server permission.</p>"
                '<a class="login" href="/dash/login">Log in with Discord</a>'
            )
            return self._html(render.page("Dashboard", body))
        rows = [
            [f'<a href="/dash/guild/{gid}">{render.esc(name or gid)}</a>', str(gid)]
            for gid, name in sorted(session.guilds.items(), key=lambda kv: kv[1].lower())
        ]
        sections = [render.table(["Server", "Guild ID"], rows, empty="No servers to show.")]
        if session.is_owner:
            sections.append(
                "<h2>Owner</h2><p>"
                '<a href="/dash/global">Global overview</a> · '
                '<a href="/dash/global/hashes">Global hash queue</a> · '
                '<a href="/dash/global/servers">Trusted servers</a></p>'
                '<p class="muted">As the deployment owner you can also open any '
                "guild page by its id: <code>/dash/guild/&lt;id&gt;</code></p>"
            )
        return self._html(render.page("Your servers", "".join(sections), user=session.username))

    async def handle_login(self, _request: web.Request) -> web.Response:
        """Set the CSRF state cookie and bounce to Discord's consent screen."""
        state = secrets.token_urlsafe(32)
        response = web.Response(
            status=302, headers={"Location": self._oauth.authorize_url(state=state)}
        )
        token = sign_payload(
            {"st": state, "exp": time.time() + _STATE_TTL_SECONDS}, secret=self._secret
        )
        self._set_cookie(response, STATE_COOKIE, token, max_age=int(_STATE_TTL_SECONDS))
        return response

    async def handle_callback(self, request: web.Request) -> web.Response:
        """Complete the OAuth flow: verify state, resolve roles, set the cookie."""
        error = request.query.get("error")
        if error:
            return self._html(
                render.page(
                    "Login failed",
                    f'<p class="muted">Discord returned: {render.esc(error)}</p>'
                    '<p><a href="/dash">Back</a></p>',
                ),
                status=400,
            )
        code = request.query.get("code", "")
        state = request.query.get("state", "")
        state_payload = verify_payload(request.cookies.get(STATE_COOKIE, ""), secret=self._secret)
        if not code or state_payload is None or state_payload.get("st") != state:
            return self._html(
                render.page(
                    "Login failed",
                    '<p class="muted">State mismatch or expired login attempt. '
                    'Please try again.</p><p><a href="/dash/login">Retry login</a></p>',
                ),
                status=400,
            )
        try:
            access_token = await self._oauth.exchange_code(code)
            user = await self._oauth.fetch_user(access_token)
            user_guilds = await self._oauth.fetch_guilds(access_token)
        except Exception:
            _log.exception("dashboard_oauth_failed")
            return self._html(
                render.page(
                    "Login failed",
                    '<p class="muted">Could not complete the Discord login. '
                    'Please try again.</p><p><a href="/dash/login">Retry login</a></p>',
                ),
                status=502,
            )

        async with self._scope() as db:
            bot_guild_ids = set(await GuildListRepository(db).all_ids())
        mod_guilds = {
            g.id: g.name
            for g in user_guilds
            if g.id in bot_guild_ids and can_moderate(g.permissions)
        }
        try:
            owner_ids = await self._fetch_owner_ids()
        except Exception:
            # Fail closed on the owner bit: a mod can still use their guild
            # pages even if the owner lookup hiccups.
            _log.exception("dashboard_owner_lookup_failed")
            owner_ids = set()
        is_owner = user.id in owner_ids

        if not is_owner and not mod_guilds:
            return self._html(
                render.page(
                    "No access",
                    '<p class="muted">Your Discord account has no Manage Server '
                    "permission in any server this bot is installed in.</p>"
                    '<p><a href="/dash">Back</a></p>',
                ),
                status=403,
            )

        session = DashboardSession(
            user_id=user.id,
            username=user.username,
            guilds=mod_guilds,
            is_owner=is_owner,
            expires_at=time.time() + self._session_ttl,
        )
        response = web.Response(status=302, headers={"Location": "/dash"})
        self._set_cookie(
            response,
            SESSION_COOKIE,
            session.to_token(secret=self._secret),
            max_age=int(self._session_ttl),
        )
        response.del_cookie(STATE_COOKIE, path="/dash")
        _log.info(
            "dashboard_login",
            user_id=user.id,
            guilds=len(mod_guilds),
            is_owner=is_owner,
        )
        return response

    async def handle_logout(self, _request: web.Request) -> web.Response:
        """Drop the session cookie and return to the landing page."""
        response = web.Response(status=302, headers={"Location": "/dash"})
        response.del_cookie(SESSION_COOKIE, path="/dash")
        return response

    # --- Guild pages -----------------------------------------------------------------

    def _guild_id(self, request: web.Request, session: DashboardSession) -> int:
        try:
            guild_id = int(request.match_info["guild_id"])
        except (KeyError, ValueError) as exc:
            raise web.HTTPNotFound from exc
        if not session.can_view_guild(guild_id):
            raise web.HTTPForbidden(
                text=render.page(
                    "Not authorized",
                    '<p class="muted">You do not moderate this server.</p>'
                    '<p><a href="/dash">Back</a></p>',
                    user=session.username,
                ),
                content_type="text/html",
            )
        return guild_id

    def _guild_nav(self, guild_id: int) -> str:
        return (
            f'<div class="tabs"><a href="/dash/guild/{guild_id}">Activity</a>'
            f'<a href="/dash/guild/{guild_id}/audit">Audit log</a>'
            '<a href="/dash">All servers</a></div>'
        )

    async def handle_guild(self, request: web.Request) -> web.Response:
        """Guild overview: 30-day chart, verdict counts, filterable detections."""
        session = self._require_session(request)
        guild_id = self._guild_id(request, session)
        verdict = request.query.get("verdict") or None
        uploader_raw = request.query.get("uploader", "").strip()
        before_raw = request.query.get("before", "").strip()
        uploader_id = int(uploader_raw) if uploader_raw.isdigit() else None
        before_id = int(before_raw) if before_raw.isdigit() else None

        async with self._scope() as db:
            activity = await queries.daily_activity(db, guild_id, days=30)
            counts = await queries.verdict_counts(db, guild_id, days=30)
            detections = await queries.list_detections(
                db,
                guild_id,
                verdict=verdict,
                uploader_id=uploader_id,
                before_id=before_id,
                limit=50,
            )

        total = sum(counts.values())
        flagged = total - counts.get(queries.CLEAN_VERDICT, 0)
        cards = render.stat_cards(
            [
                ("scans · 30d", total),
                ("flagged · 30d", flagged),
                ("scams · 30d", counts.get("scam", 0)),
                ("ambiguous · 30d", counts.get("ambiguous", 0)),
            ]
        )

        options = "".join(
            f'<option value="{render.esc(v)}"{" selected" if v == verdict else ""}>'
            f"{render.esc(v)}</option>"
            for v in ("clean", "ambiguous", "scam", "non_decision")
        )
        filters = (
            f'<form class="filters" method="get" action="/dash/guild/{guild_id}">'
            f'<label>Verdict <select name="verdict"><option value="">all</option>'
            f"{options}</select></label>"
            f'<label>Uploader ID <input name="uploader" inputmode="numeric" '
            f'value="{render.esc(uploader_raw)}" placeholder="user id"></label>'
            "<button>Filter</button></form>"
        )

        rows = []
        for d in detections:
            rows.append(
                [
                    f'<a href="/dash/guild/{guild_id}/detection/{d.id}">#{d.id}</a>',
                    render.esc(d.created_at.strftime("%Y-%m-%d %H:%M")),
                    render.verdict_badge(d.verdict),
                    render.esc(d.action_taken),
                    f"<code>{d.uploader_id}</code>",
                    f"<code>{d.channel_id}</code>",
                ]
            )
        older = ""
        if len(detections) == 50:
            last_id = detections[-1].id
            query = [f"before={last_id}"]
            if verdict:
                query.append(f"verdict={render.esc(verdict)}")
            if uploader_raw.isdigit():
                query.append(f"uploader={uploader_raw}")
            older = f'<p><a href="/dash/guild/{guild_id}?{"&".join(query)}">Older →</a></p>'

        body = (
            self._guild_nav(guild_id)
            + cards
            + "<h2>Scan activity · 30 days</h2>"
            + render.activity_chart(activity)
            + "<h2>Detections</h2>"
            + filters
            + render.table(
                ["ID", "When (UTC)", "Verdict", "Action", "Uploader", "Channel"],
                rows,
                empty="No detections match.",
            )
            + older
        )
        return self._html(render.page(f"Server {guild_id}", body, user=session.username))

    async def handle_detection(self, request: web.Request) -> web.Response:
        """Full record of one detection: ids, verdict, distances, hashes."""
        session = self._require_session(request)
        guild_id = self._guild_id(request, session)
        try:
            detection_id = int(request.match_info["detection_id"])
        except (KeyError, ValueError) as exc:
            raise web.HTTPNotFound from exc
        async with self._scope() as db:
            detection = await queries.get_detection(db, guild_id, detection_id)
        if detection is None:
            raise web.HTTPNotFound(
                text=render.page(
                    "Not found",
                    '<p class="muted">No such detection in this server.</p>'
                    f'<p><a href="/dash/guild/{guild_id}">Back</a></p>',
                    user=session.username,
                ),
                content_type="text/html",
            )
        hashes = detection.hashes or {}
        hash_rows = "".join(
            f"<dt>{render.esc(k)}</dt><dd><code>{render.esc(v)}</code></dd>"
            for k, v in sorted(hashes.items())
        )
        body = (
            self._guild_nav(guild_id)
            + '<dl class="kv">'
            + f"<dt>Detection</dt><dd>#{detection.id}</dd>"
            + f"<dt>When (UTC)</dt><dd>{render.esc(detection.created_at.isoformat())}</dd>"
            + f"<dt>Verdict</dt><dd>{render.verdict_badge(detection.verdict)}</dd>"
            + f"<dt>Action taken</dt><dd>{render.esc(detection.action_taken)}</dd>"
            + f"<dt>Uploader</dt><dd><code>{detection.uploader_id}</code></dd>"
            + f"<dt>Channel</dt><dd><code>{detection.channel_id}</code></dd>"
            + f"<dt>Message</dt><dd><code>{detection.message_id}</code></dd>"
            + f"<dt>Attachment</dt><dd><code>{detection.attachment_id}</code></dd>"
            + "<dt>Distances</dt><dd><code>"
            + render.esc(queries.summarize_distances(detection.distances) or "—")
            + "</code></dd>"
            + (hash_rows or "<dt>Hashes</dt><dd>—</dd>")
            + "</dl>"
            + '<p class="muted">Images are never stored or displayed; only '
            "perceptual hashes and match distances are kept.</p>"
        )
        return self._html(render.page(f"Detection #{detection.id}", body, user=session.username))

    async def handle_audit(self, request: web.Request) -> web.Response:
        """Guild audit log: every recorded moderator/config action."""
        session = self._require_session(request)
        guild_id = self._guild_id(request, session)
        async with self._scope() as db:
            actions = await queries.list_mod_actions(db, guild_id, limit=200)
        rows = [
            [
                render.esc(a.created_at.strftime("%Y-%m-%d %H:%M")),
                render.esc(a.action),
                f"<code>{a.actor_id}</code>",
                render.esc(a.target or "—"),
                f"<code>{render.esc(a.payload)}</code>" if a.payload else "—",
            ]
            for a in actions
        ]
        body = self._guild_nav(guild_id) + render.table(
            ["When (UTC)", "Action", "Actor", "Target", "Details"],
            rows,
            empty="No audit entries yet.",
        )
        return self._html(
            render.page(f"Audit log · server {guild_id}", body, user=session.username)
        )

    # --- Owner pages -------------------------------------------------------------------

    def _require_owner(self, request: web.Request) -> DashboardSession:
        session = self._require_session(request)
        if not session.is_owner:
            raise web.HTTPForbidden(
                text=render.page(
                    "Not authorized",
                    '<p class="muted">Global pages are restricted to the '
                    'deployment owner.</p><p><a href="/dash">Back</a></p>',
                    user=session.username,
                ),
                content_type="text/html",
            )
        return session

    async def handle_global(self, request: web.Request) -> web.Response:
        """Owner overview: per-guild activity and global hash totals."""
        session = self._require_owner(request)
        async with self._scope() as db:
            overview = await queries.guild_overview(db, days=7)
            hash_counts = await queries.global_hash_status_counts(db)
        cards = render.stat_cards(
            [
                ("candidate hashes", hash_counts.get("candidate", 0)),
                ("promoted hashes", hash_counts.get("promoted", 0)),
                ("revoked hashes", hash_counts.get("revoked", 0)),
                ("active servers · 7d", len(overview)),
            ]
        )
        rows = [
            [
                f'<a href="/dash/guild/{row.guild_id}">{row.guild_id}</a>',
                str(row.total),
                str(row.flagged),
            ]
            for row in overview
        ]
        body = (
            '<div class="tabs"><a href="/dash/global/hashes">Global hash queue</a>'
            '<a href="/dash/global/servers">Trusted servers</a>'
            '<a href="/dash">All servers</a></div>'
            + cards
            + "<h2>Scans by server · 7 days</h2>"
            + render.table(
                ["Server", "Scans", "Flagged"], rows, empty="No scans in the last 7 days."
            )
        )
        return self._html(render.page("Global overview", body, user=session.username))

    async def handle_global_hashes(self, request: web.Request) -> web.Response:
        """The global hash database, filterable by lifecycle status."""
        session = self._require_owner(request)
        status = request.query.get("status", "candidate")
        if status not in _HASH_STATUSES:
            status = "candidate"
        async with self._scope() as db:
            hashes = await queries.list_global_hashes(db, status=status, limit=200)
        tabs = " · ".join(
            f"<strong>{s}</strong>"
            if s == status
            else f'<a href="/dash/global/hashes?status={s}">{s}</a>'
            for s in _HASH_STATUSES
        )
        rows = [
            [
                f"<code>{render.esc(row.hash.hash_id)}</code>",
                render.esc(row.hash.created_at.strftime("%Y-%m-%d %H:%M")),
                f"{row.votes} vote(s) from {row.distinct_guilds} server(s)",
                f"<code>{row.hash.submitter_guild_id or '—'}</code>",
                render.esc(row.hash.campaign_id or "—"),
            ]
            for row in hashes
        ]
        body = (
            f"<p>{tabs}</p>"
            + render.table(
                ["Hash", "Submitted (UTC)", "Approvals", "Source server", "Campaign"],
                rows,
                empty=f"No {status} hashes.",
            )
            + '<p class="muted">Promotion requires approvals from 2 distinct '
            "servers. Voting itself stays in Discord (review-channel buttons).</p>"
        )
        return self._html(render.page(f"Global hashes · {status}", body, user=session.username))

    async def handle_global_servers(self, request: web.Request) -> web.Response:
        """Servers allow-listed to vote on the global hash database."""
        session = self._require_owner(request)
        async with self._scope() as db:
            trusted = await queries.list_trusted_guilds(db)
        rows = [
            [
                f"<code>{g.guild_id}</code>",
                f"<code>{g.added_by}</code>",
                render.esc(g.created_at.strftime("%Y-%m-%d %H:%M")),
            ]
            for g in trusted
        ]
        body = render.table(
            ["Server", "Approved by", "Since (UTC)"],
            rows,
            empty="No trusted servers yet. Use /global approve_server in Discord.",
        )
        return self._html(render.page("Trusted servers", body, user=session.username))


def build_dashboard(
    settings: Settings,
    *,
    client_id: str,
    scope: SessionScope,
    fetch_owner_ids: Callable[[], Awaitable[set[int]]],
) -> DashboardService | None:
    """Construct the dashboard from settings, or ``None`` when disabled/unusable.

    Startup validation (:func:`optimus.app.startup.validate_simple_startup`)
    already rejects half-configured dashboards with an actionable message in
    simple mode; the re-check here is the fail-safe for other entry points, so
    a misconfiguration degrades to "no dashboard" rather than broken routes.
    """
    if not settings.dashboard_enabled:
        return None
    base = settings.dashboard_base_url.rstrip("/")
    if (
        not settings.discord_client_secret
        or not base.startswith(("http://", "https://"))
        or len(settings.dashboard_session_secret) < 32
    ):
        _log.warning("dashboard_misconfigured_disabled")
        return None
    oauth = DiscordOAuthClient(
        client_id=client_id,
        client_secret=settings.discord_client_secret,
        redirect_uri=f"{base}/dash/callback",
    )
    return DashboardService(
        secret=settings.dashboard_session_secret,
        session_ttl_seconds=settings.dashboard_session_ttl_seconds,
        secure_cookies=base.startswith("https://"),
        oauth=oauth,
        scope=scope,
        fetch_owner_ids=fetch_owner_ids,
    )
