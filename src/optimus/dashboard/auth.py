"""Discord OAuth2 login and signed-cookie sessions for the dashboard.

The dashboard authenticates moderators with Discord's standard OAuth2
authorization-code flow (``identify`` + ``guilds`` scopes) and then keeps a
short, HMAC-signed session cookie. No OAuth access token is stored anywhere:
it is used once at login to resolve *who this is* and *which servers they can
moderate*, and discarded. Everything the request handlers need afterwards
(user id, mod-visible guilds, owner flag, expiry) lives inside the signed
cookie payload, so there is no server-side session table to manage or leak.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import aiohttp

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_API_BASE = "https://discord.com/api/v10"

#: Discord permission bits that grant moderator access to a guild's pages.
#: MANAGE_GUILD is the same bar the bot's own slash commands use for
#: mod-level commands; ADMINISTRATOR implies everything.
PERM_ADMINISTRATOR = 1 << 3
PERM_MANAGE_GUILD = 1 << 5


def can_moderate(permissions: int) -> bool:
    """Whether an OAuth guild ``permissions`` mask grants dashboard access."""
    return bool(permissions & (PERM_ADMINISTRATOR | PERM_MANAGE_GUILD))


# --- Signed payloads -------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def sign_payload(payload: dict[str, Any], *, secret: str) -> str:
    """Serialize ``payload`` into a tamper-evident ``body.signature`` token.

    The body is URL-safe base64 of the JSON payload; the signature is
    HMAC-SHA256 over the body bytes keyed with ``secret``. The payload is
    *readable* by whoever holds the cookie (it is their own session data);
    the HMAC only prevents forging or altering it.
    """
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(sig)}"


def verify_payload(token: str, *, secret: str, now: float | None = None) -> dict[str, Any] | None:
    """Verify a token produced by :func:`sign_payload`; ``None`` when invalid.

    Rejects malformed tokens, bad signatures (constant-time compare), payloads
    that are not JSON objects, and payloads whose ``exp`` (unix seconds) has
    passed. A missing ``exp`` is invalid by construction — every payload this
    module signs carries one, so its absence means the token was not ours.
    """
    body, sep, sig_text = token.partition(".")
    if not sep or not body or not sig_text:
        return None
    try:
        provided = _b64decode(sig_text)
    except (binascii.Error, ValueError):
        return None
    expected = hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(provided, expected):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int | float):
        return None
    if (now if now is not None else time.time()) >= float(exp):
        return None
    return payload


# --- Session model ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DashboardSession:
    """An authenticated dashboard visitor, as carried in the session cookie.

    ``guilds`` maps the guild ids this user may view (moderator permission on
    Discord's side AND known to this bot) to their display names. ``is_owner``
    marks the deployment owner (Discord application owner / team member), who
    can additionally view every guild page and the global sections.
    """

    user_id: int
    username: str
    guilds: dict[int, str]
    is_owner: bool
    expires_at: float

    def can_view_guild(self, guild_id: int) -> bool:
        """Whether this session may view ``guild_id``'s pages."""
        return self.is_owner or guild_id in self.guilds

    def to_token(self, *, secret: str) -> str:
        """Serialize into a signed cookie value."""
        payload = {
            "uid": self.user_id,
            "un": self.username,
            "g": {str(gid): name for gid, name in self.guilds.items()},
            "o": self.is_owner,
            "exp": self.expires_at,
        }
        return sign_payload(payload, secret=secret)

    @classmethod
    def from_token(
        cls, token: str, *, secret: str, now: float | None = None
    ) -> DashboardSession | None:
        """Parse and verify a cookie value; ``None`` when invalid or expired."""
        payload = verify_payload(token, secret=secret, now=now)
        if payload is None:
            return None
        try:
            guilds_raw = payload.get("g") or {}
            if not isinstance(guilds_raw, dict):
                return None
            return cls(
                user_id=int(payload["uid"]),
                username=str(payload.get("un", "")),
                guilds={int(gid): str(name) for gid, name in guilds_raw.items()},
                is_owner=bool(payload.get("o", False)),
                expires_at=float(payload["exp"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


# --- OAuth client -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OAuthUser:
    """The ``identify``-scope subset of a Discord user we care about."""

    id: int
    username: str


@dataclass(frozen=True, slots=True)
class OAuthGuild:
    """One entry of ``GET /users/@me/guilds``: a guild and the user's perms in it."""

    id: int
    name: str
    permissions: int


class OAuthClient(Protocol):
    """The OAuth surface the dashboard handlers depend on (fakeable in tests)."""

    def authorize_url(self, *, state: str) -> str:
        """The Discord consent-screen URL to redirect a visitor to."""
        ...

    async def exchange_code(self, code: str) -> str:
        """Exchange an authorization code for a bearer access token."""
        ...

    async def fetch_user(self, access_token: str) -> OAuthUser:
        """Fetch the logged-in user's id and username."""
        ...

    async def fetch_guilds(self, access_token: str) -> list[OAuthGuild]:
        """Fetch the guilds the user is in, with their permission masks."""
        ...

    async def close(self) -> None:
        """Release any underlying HTTP resources."""
        ...


class DiscordOAuthClient:
    """Real :class:`OAuthClient` speaking to Discord's OAuth2 endpoints."""

    def __init__(self, *, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http: aiohttp.ClientSession | None = None

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10.0))
        return self._http

    def authorize_url(self, *, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "response_type": "code",
                "scope": "identify guilds",
                "redirect_uri": self._redirect_uri,
                "state": state,
                "prompt": "none",
            }
        )
        return f"{DISCORD_AUTHORIZE_URL}?{query}"

    async def exchange_code(self, code: str) -> str:
        async with self._session().post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("Discord token response had no access_token")
        return token

    async def fetch_user(self, access_token: str) -> OAuthUser:
        async with self._session().get(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return OAuthUser(id=int(data["id"]), username=str(data.get("username", "")))

    async def fetch_guilds(self, access_token: str) -> list[OAuthGuild]:
        async with self._session().get(
            f"{DISCORD_API_BASE}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        guilds: list[OAuthGuild] = []
        for entry in data:
            try:
                guilds.append(
                    OAuthGuild(
                        id=int(entry["id"]),
                        name=str(entry.get("name", "")),
                        permissions=int(entry.get("permissions", 0)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return guilds

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None
