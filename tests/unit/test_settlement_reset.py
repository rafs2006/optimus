"""A moderator's False positive (or Whitelist image, Dismiss) ends a settlement.

Once an uploader is settled, their later flagged images are deleted without a
card (see test_settled_uploader.py). The buttons that take that call back run
in the interactions service, possibly another process, so the coordinator asks
the database before each quiet deletion: if a moderator cleared any of the
uploader's cards since the settlement, it forgets the settlement and the image
gets a normal card again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from structlog.testing import capture_logs

from optimus.contracts.events import Action
from tests.unit.test_confirm_campaign_cleanup import scope as _scope_fixture
from tests.unit.test_departed_ban_auto_close import _departed, _event, _Harness


def _with_clears(h: _Harness, answer: bool | Exception) -> list[datetime]:
    asked: list[datetime] = []

    async def cleared(_guild: int, _uploader: int, since: datetime) -> bool:
        asked.append(since)
        if isinstance(answer, Exception):
            raise answer
        return answer

    h.coord._settlement_cleared = cleared
    return asked


async def test_false_positive_on_the_settled_card_brings_cards_back() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    asked = _with_clears(h, True)
    with capture_logs() as logs:
        result = await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))

    assert result.action is not Action.DELETE  # not removed quietly
    assert len(h.reports) == 2  # a normal card for a moderator
    assert any(e["event"] == "settlement_reversed" for e in logs)
    # Forgotten: settled, banned and the campaign all go.
    assert (1, 42) not in h.coord._settled
    assert (1, 42) not in h.coord._banned_uploaders
    assert (1, 42) not in h.coord._campaigns
    # Asked from the settlement on, not from some fixed window.
    assert asked and datetime.now(UTC) - asked[0] < timedelta(minutes=1)


async def test_no_clear_keeps_the_quiet_removal() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    _with_clears(h, False)
    result = await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))
    assert result.action is Action.DELETE
    assert len(h.reports) == 1


async def test_a_failed_check_sends_the_image_to_a_moderator_but_keeps_the_settlement() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    _with_clears(h, RuntimeError("db down"))
    with capture_logs() as logs:
        await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))
    assert len(h.reports) == 2
    assert any(e["event"] == "settlement_check_failed" for e in logs)
    assert (1, 42) in h.coord._settled


async def test_late_card_is_left_open_after_a_false_positive() -> None:
    h = _Harness(target=_departed())
    h.coord._mark_settled(_event(message_id=3), banned=True)
    _with_clears(h, True)
    await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))
    assert h.closes == []  # the open card stays for a moderator


async def test_without_the_hook_nothing_changes() -> None:
    h = _Harness(target=_departed())
    h.coord._settlement_cleared = None
    await h.coord.handle_verdict(_event(message_id=3))
    result = await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))
    assert result.action is Action.DELETE


db_scope = _scope_fixture


async def test_the_wired_check_counts_only_clears_after_the_settlement(db_scope: Any) -> None:
    import fakeredis.aioredis

    from optimus.core.config import get_settings
    from optimus.db.models import Detection, Guild, ModAction
    from optimus.services.moderation.service import build_coordinator

    settled_at = datetime.now(UTC)
    async with db_scope() as s:
        s.add(Guild(guild_id=7))
        # The settled card's image was detected 20 minutes before the Confirm.
        det = Detection(
            guild_id=7,
            channel_id=201,
            message_id=10,
            attachment_id=1,
            uploader_id=42,
            distances={},
            verdict="scam",
            idempotency_key="reset-1",
            action_taken="confirmed",
            created_at=settled_at - timedelta(minutes=20),
        )
        s.add(det)
        await s.flush()
        target = str(det.id)
        # Before the settlement: an older Dismiss and the Confirm itself.
        for action, at in (
            ("review.dismiss", settled_at - timedelta(minutes=5)),
            ("review.confirm_scam", settled_at + timedelta(seconds=1)),
        ):
            s.add(ModAction(guild_id=7, actor_id=5, action=action, target=target, created_at=at))
    coord, _dispatcher = build_coordinator(
        get_settings(),
        db_scope,
        rest=object(),
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        bot_user_id=999,
    )
    check = coord._settlement_cleared
    assert check is not None
    assert await check(7, 42, settled_at) is False

    async with db_scope() as s:
        s.add(
            ModAction(
                guild_id=7,
                actor_id=5,
                action="review.false_positive",
                target=target,
                created_at=settled_at + timedelta(minutes=2),
            )
        )
    assert await check(7, 42, settled_at) is True
    assert await check(7, 43, settled_at) is False  # another uploader
