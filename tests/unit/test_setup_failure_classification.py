"""``DbDeps.rest_create_review_channel`` against real hikari exceptions.

The handler tests drive :class:`~optimus.services.interactions.handlers.SetupFailure`
values directly through a fake, so they cannot catch the part that actually
broke in production: every refusal used to collapse into "grant the bot Manage
Channels", which is the wrong instruction for a guild at Discord's channel cap,
for an overwrite cap tripped by ``mod_role``, for a rate limit, and for a
Discord outage.

These tests construct genuine ``hikari`` exception instances rather than
protocol-shaped doubles, so a hikari signature or hierarchy change fails the
suite instead of production -- the same discipline
:mod:`optimus.services.moderation.rest_adapter` uses.
"""

from __future__ import annotations

import http

import hikari
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.core.config import get_settings
from optimus.core.ratelimit import RateLimit
from optimus.services.interactions.handlers import SetupFailure
from optimus.services.interactions.service import DbDeps

GUILD_ID = 424242424242424242


class _NoopRateLimiter:
    async def acquire(self, key: str, limit: RateLimit, cost: float = 1.0) -> bool:
        return True


class _RaisingRest:
    """A ``ModerationRest`` stand-in whose channel creation always fails."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def create_review_channel(
        self, guild_id: int, *, name: str, mod_role_ids: list[int]
    ) -> int:
        raise self._exc


class _CreatingRest:
    async def create_review_channel(
        self, guild_id: int, *, name: str, mod_role_ids: list[int]
    ) -> int:
        return 777


URL = "https://discord.com/api/v10/guilds/1/channels"


def _client_error(cls: type[hikari.ClientHTTPResponseError], code: int) -> Exception:
    """Build a 4xx error.

    hikari's client-side subclasses hardcode their own ``status``, so unlike
    :class:`hikari.InternalServerError` they reject a ``status`` argument.
    """
    return cls(URL, {}, b"{}", "boom", code)


def _server_error(code: int) -> Exception:
    return hikari.InternalServerError(
        URL, http.HTTPStatus.INTERNAL_SERVER_ERROR, {}, b"{}", "boom", code
    )


def _make_deps(session: AsyncSession, rest: object | None) -> DbDeps:
    return DbDeps(session, _NoopRateLimiter(), get_settings(), rest=rest)  # type: ignore[arg-type]


async def test_successful_creation_returns_the_channel_id(session: AsyncSession) -> None:
    result = await _make_deps(session, _CreatingRest()).rest_create_review_channel(
        GUILD_ID, name="optimus-review", mod_role_ids=[]
    )
    assert result.channel_id == 777
    assert result.failure is None


async def test_missing_rest_reports_unavailable_not_a_permission_problem(
    session: AsyncSession,
) -> None:
    """A deployment without REST cannot create channels; no permission will fix it."""
    result = await _make_deps(session, None).rest_create_review_channel(
        GUILD_ID, name="optimus-review", mod_role_ids=[]
    )
    assert result.channel_id is None
    assert result.failure is SetupFailure.UNAVAILABLE


@pytest.mark.parametrize(
    ("name", "exc", "expected"),
    [
        # Missing Manage Channels -- the only case the old blanket advice fit.
        (
            "missing_permissions",
            _client_error(hikari.ForbiddenError, 50013),
            SetupFailure.NO_PERMISSION,
        ),
        # Missing access: also a 403, but a different JSON code.
        (
            "missing_access",
            _client_error(hikari.ForbiddenError, 50001),
            SetupFailure.NO_PERMISSION,
        ),
        # At Discord's 500-channel cap -- arrives as a plain 400.
        (
            "channel_cap",
            _client_error(hikari.BadRequestError, 30013),
            SetupFailure.CHANNEL_LIMIT,
        ),
        # mod_role blew the 1000-overwrite cap -- also a plain 400, so only the
        # JSON code separates it from the channel cap above.
        (
            "overwrite_cap",
            _client_error(hikari.BadRequestError, 30060),
            SetupFailure.OVERWRITE_LIMIT,
        ),
        # A token problem still reads as "the bot was refused".
        (
            "unauthorized",
            _client_error(hikari.UnauthorizedError, 0),
            SetupFailure.NO_PERMISSION,
        ),
        # Discord's fault, nothing to fix on the server.
        ("discord_5xx", _server_error(0), SetupFailure.DISCORD_DOWN),
    ],
)
async def test_each_discord_error_maps_to_its_own_failure(
    session: AsyncSession,
    name: str,
    exc: Exception,
    expected: SetupFailure,
) -> None:
    result = await _make_deps(session, _RaisingRest(exc)).rest_create_review_channel(
        GUILD_ID, name="optimus-review", mod_role_ids=[]
    )
    assert result.channel_id is None
    assert result.failure is expected


async def test_unmapped_bad_request_does_not_blame_permissions(
    session: AsyncSession,
) -> None:
    """The regression this whole change exists to fix.

    A malformed request is a 400 with a code we do not map. Sending moderators
    to grant Manage Channels for it is exactly the wrong-advice bug -- it must
    fall through to the generic reply instead.
    """
    rest = _RaisingRest(_client_error(hikari.BadRequestError, 50035))
    result = await _make_deps(session, rest).rest_create_review_channel(
        GUILD_ID, name="optimus-review", mod_role_ids=[]
    )
    assert result.failure is SetupFailure.UNKNOWN


async def test_rate_limit_carries_the_retry_hint(session: AsyncSession) -> None:
    rest = _RaisingRest(
        hikari.RateLimitTooLongError(
            route="POST /guilds/{guild}/channels",  # type: ignore[arg-type]
            is_global=False,
            retry_after=125.4,
            max_retry_after=60.0,
            reset_at=0.0,
            limit=5,
            period=60.0,
        )
    )
    result = await _make_deps(session, rest).rest_create_review_channel(
        GUILD_ID, name="optimus-review", mod_role_ids=[]
    )
    assert result.failure is SetupFailure.RATE_LIMITED
    # Must come from ``retry_after``: ``remaining`` counts requests left in the
    # window and is hardcoded to 0, which would flatten every rate limit into
    # the same "try again in 1 minute".
    assert result.retry_seconds == 125


async def test_unexpected_exception_falls_back_to_generic_advice(
    session: AsyncSession,
) -> None:
    """A non-HTTP failure (DNS, TLS, a bug) must not crash the interaction."""
    rest = _RaisingRest(RuntimeError("socket exploded"))
    result = await _make_deps(session, rest).rest_create_review_channel(
        GUILD_ID, name="optimus-review", mod_role_ids=[]
    )
    assert result.channel_id is None
    assert result.failure is SetupFailure.UNKNOWN
