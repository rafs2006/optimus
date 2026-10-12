"""Who a punitive action would hit: the target's roles, or why they are not a member."""

from __future__ import annotations

from optimus.core.logging import get_logger
from optimus.services.moderation.boundaries import TargetContext

_log = get_logger(__name__)


async def resolve_target(  # pragma: no cover - requires live REST
    rest: object, guild_id: int, user_id: int, bot_user_id: int
) -> TargetContext | None:
    import hikari

    try:
        member = await rest.fetch_member(guild_id, user_id)  # type: ignore[attr-defined]
    except hikari.NotFoundError:
        # Genuinely absent: the uploader left or was already banned. Reported
        # as an out-of-guild target rather than ``None`` so the caller can tell
        # "gone" (a ban by user id is still possible and safe: a non-member
        # holds no roles) from "unverifiable" (never punish blind).
        already_banned = await is_banned(rest, guild_id, user_id)
        _log.info(
            "target_resolve_not_found",
            guild_id=guild_id,
            user_id=user_id,
            already_banned=already_banned,
        )
        return TargetContext(
            user_id=user_id,
            guild_owner_id=0,
            bot_user_id=bot_user_id,
            is_administrator=False,
            top_role_position=0,
            bot_top_role_position=0,
            in_guild=False,
            already_banned=already_banned,
        )
    except Exception:
        _log.warning(
            "target_resolve_failed",
            guild_id=guild_id,
            user_id=user_id,
            exc_info=True,
        )
        return None
    try:
        guild = await rest.fetch_guild(guild_id)  # type: ignore[attr-defined]
        roles = await rest.fetch_roles(guild_id)  # type: ignore[attr-defined]
        me = await rest.fetch_member(guild_id, bot_user_id)  # type: ignore[attr-defined]
    except Exception:
        # Anything else (a 403 from missing permissions, a transient 5xx) used
        # to propagate out of the coordinator and abandon the verdict entirely:
        # no delete, no ban, no report, just a stack trace. Failing closed to
        # "privileges unverifiable" downgrades to delete-only instead, so the
        # scam still comes down while the cause stays visible in the logs.
        _log.warning(
            "target_resolve_failed",
            guild_id=guild_id,
            user_id=user_id,
            exc_info=True,
        )
        return None
    by_id = {int(r.id): r for r in roles}
    is_admin = any(
        (by_id[int(rid)].permissions & hikari.Permissions.ADMINISTRATOR)
        for rid in member.role_ids
        if int(rid) in by_id
    )
    top = max((by_id[int(rid)].position for rid in member.role_ids if int(rid) in by_id), default=0)
    bot_top = max((by_id[int(rid)].position for rid in me.role_ids if int(rid) in by_id), default=0)
    return TargetContext(
        user_id=user_id,
        guild_owner_id=int(guild.owner_id),
        bot_user_id=bot_user_id,
        is_administrator=is_admin,
        top_role_position=top,
        bot_top_role_position=bot_top,
    )


async def is_banned(rest: object, guild_id: int, user_id: int) -> bool:  # pragma: no cover
    """Whether Discord holds a ban for ``user_id``; ``False`` when unknown.

    Only asked for a target who is not a member, so the card can tell "already
    banned" (by this bot moments ago, or by a moderator) from "left on their
    own". Any failure reads as "not banned": the ban by user id still runs and
    the card falls back to the departed wording.
    """
    try:
        await rest.fetch_ban(guild_id, user_id)  # type: ignore[attr-defined]
    except Exception:
        return False
    return True
