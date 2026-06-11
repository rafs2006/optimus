"""Unit tests for moderation privilege boundaries."""

from __future__ import annotations

from optimus.services.moderation.boundaries import (
    BoundaryRefusal,
    TargetContext,
    check_target,
)


def _ctx(**kw: object) -> TargetContext:
    base: dict[str, object] = {
        "user_id": 100,
        "guild_owner_id": 1,
        "bot_user_id": 2,
        "is_administrator": False,
        "top_role_position": 1,
        "bot_top_role_position": 5,
        "in_guild": True,
    }
    base.update(kw)
    return TargetContext(**base)  # type: ignore[arg-type]


def test_ordinary_member_is_allowed() -> None:
    result = check_target(_ctx())
    assert result.allowed
    assert result.refusal is None


def test_dm_is_refused() -> None:
    result = check_target(_ctx(in_guild=False))
    assert not result.allowed
    assert result.refusal is BoundaryRefusal.NOT_IN_GUILD


def test_self_is_refused() -> None:
    result = check_target(_ctx(user_id=2, bot_user_id=2))
    assert not result.allowed
    assert result.refusal is BoundaryRefusal.SELF


def test_guild_owner_is_refused() -> None:
    result = check_target(_ctx(user_id=1, guild_owner_id=1))
    assert not result.allowed
    assert result.refusal is BoundaryRefusal.GUILD_OWNER


def test_administrator_is_refused() -> None:
    result = check_target(_ctx(is_administrator=True))
    assert not result.allowed
    assert result.refusal is BoundaryRefusal.ADMINISTRATOR


def test_role_at_or_above_bot_is_refused() -> None:
    equal = check_target(_ctx(top_role_position=5, bot_top_role_position=5))
    assert not equal.allowed
    assert equal.refusal is BoundaryRefusal.ROLE_HIERARCHY
    above = check_target(_ctx(top_role_position=9, bot_top_role_position=5))
    assert not above.allowed
    assert above.refusal is BoundaryRefusal.ROLE_HIERARCHY


def test_role_just_below_bot_is_allowed() -> None:
    result = check_target(_ctx(top_role_position=4, bot_top_role_position=5))
    assert result.allowed


def test_self_check_precedes_owner_check() -> None:
    # A bot that is also somehow the owner is refused as SELF first.
    result = check_target(_ctx(user_id=2, bot_user_id=2, guild_owner_id=2))
    assert result.refusal is BoundaryRefusal.SELF
