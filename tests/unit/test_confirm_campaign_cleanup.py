"""One confirmation settles an uploader's whole campaign.

A scammer pastes the same picture into many channels, which used to leave one
review card per message for a moderator to click through. Now a moderator's
confirmation (Confirm scam, "Review as scam") sweeps that uploader's other
recent posts whatever the server's ``action_policy``, closes and deletes their
other open cards, and their later blocklisted reposts are removed without a
card of their own -- counted on the confirmed card instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession as _Session

from optimus.contracts.events import Action, Verdict, VerdictEvent
from optimus.core.config import get_settings
from optimus.db.engine import SessionScope, create_engine, create_session_factory, session_scope
from optimus.db.models import Base, Detection, Guild, ModAction
from optimus.i18n import translate
from optimus.services.moderation import coordinator as coordinator_module
from optimus.services.moderation.coordinator import CardCleanup, ModerationCoordinator
from optimus.services.moderation.service import build_coordinator
from optimus.services.moderation.sweep import SweepOutcome
from optimus.shared.review import ReportData, build_card
from tests.unit.test_coordinator import _build, _cfg, _FakeRest, _target


def _event(message_id: int, attachment_id: int = 1, **kw: Any) -> VerdictEvent:
    base: dict[str, Any] = {
        "correlation_id": "c",
        "occurred_at": datetime.now(UTC),
        "guild_id": 1,
        "channel_id": 2,
        "message_id": message_id,
        "attachment_id": attachment_id,
        "uploader_id": 42,
        "idempotency_key": f"k:{message_id}:{attachment_id}:{kw.get('confirmed_by')}",
        "verdict": Verdict.SCAM,
        "confidence": 1.0,
    }
    base.update(kw)
    return VerdictEvent(**base)


class _Harness:
    """A real coordinator on report_only, with recording collaborators."""

    def __init__(self, **cfg: Any) -> None:
        self.rest = _FakeRest()
        self.reports: list[ReportData] = []
        self.audits: list[tuple[str, bool]] = []
        self.sweeps: list[int] = []
        self.closes: list[tuple[int, int, int, int, int | None]] = []
        self.updates: list[tuple[int, list[ReportData]]] = []
        self.stamps: list[tuple[int, int | None]] = []
        self.cleanup = CardCleanup(closed=2, cards_deleted=2, message_ids=(8, 9))

        async def sweep(event: VerdictEvent) -> SweepOutcome:
            self.sweeps.append(event.message_id)
            return SweepOutcome(deleted=2, channels=2)

        self.coord: ModerationCoordinator = _build(
            rest=self.rest,
            redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
            cfg=_cfg(configured_action=Action.REPORT_ONLY, **cfg),
            target=_target(),
            reports=self.reports,
            audits=self.audits,
            sweep=sweep,
        )
        self.coord._close_cards = self._close
        self.coord._update_report = self._update
        self.coord._mark_reported = self._stamp

    async def _close(self, *args: Any) -> CardCleanup:
        self.closes.append(args)
        return self.cleanup

    async def _update(self, _channel: int, card_id: int, items: Sequence[ReportData]) -> None:
        self.updates.append((card_id, list(items)))

    async def _stamp(self, _guild: int, detection_id: int, card_id: int | None) -> None:
        self.stamps.append((detection_id, card_id))


async def test_a_confirmation_sweeps_and_closes_cards_on_report_only() -> None:
    h = _Harness()
    await h.coord.handle_verdict(_event(3, confirmed_by=77))

    assert h.sweeps == [3]  # report_only no longer blocks a moderator's call
    assert h.closes == [(1, 42, 3, 77, 100)]
    (card,) = h.reports
    assert "purged 2 more in 2 channels" in card.action_taken
    assert "cleared 2 other report(s) from this uploader" in card.action_taken


async def test_the_other_images_of_the_card_do_not_sweep_again() -> None:
    h = _Harness()
    for attachment in (1, 2, 3):
        await h.coord.handle_verdict(_event(3, attachment, confirmed_by=77))
    assert h.sweeps == [3]
    assert len(h.closes) == 1


async def test_the_sweep_dedupe_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness()
    await h.coord.handle_verdict(_event(3, confirmed_by=77))
    monkeypatch.setattr(coordinator_module, "SWEEP_DEDUPE_SECONDS", -1)
    await h.coord.handle_verdict(_event(4, confirmed_by=77))
    assert h.sweeps == [3, 4]


async def test_an_automatic_report_only_verdict_still_does_not_sweep() -> None:
    h = _Harness()
    await h.coord.handle_verdict(_event(3))
    assert h.sweeps == []
    assert h.closes == []


async def test_closed_cards_are_forgotten_so_late_images_post_fresh() -> None:
    h = _Harness()
    h.coord._open_cards[(1, 8, False)] = coordinator_module._OpenCard(
        channel_id=100, card_id=555, items=[]
    )
    await h.coord.handle_verdict(_event(3, confirmed_by=77))
    assert (1, 8, False) not in h.coord._open_cards


async def test_a_failing_closer_leaves_the_confirmation_intact() -> None:
    h = _Harness()

    async def boom(*_a: Any) -> CardCleanup:
        raise RuntimeError("db gone")

    h.coord._close_cards = boom
    result = await h.coord.handle_verdict(_event(3, confirmed_by=77))
    assert result.success
    assert "cleared" not in h.reports[0].action_taken


async def test_a_later_blocklisted_repost_is_removed_without_a_card() -> None:
    h = _Harness()
    await h.coord.handle_verdict(_event(3, confirmed_by=77))
    assert len(h.reports) == 1

    result = await h.coord.handle_verdict(_event(20, matched_hash_id="aa", matched_source="guild"))
    assert result.action is Action.DELETE and result.success
    assert "delete_message" in h.rest.calls
    assert len(h.reports) == 1  # no second card
    card_id, items = h.updates[-1]
    assert card_id == 7
    assert items[0].followups_removed == 1
    assert h.stamps[-1] == (7, 7)  # stamped onto the confirmed card

    await h.coord.handle_verdict(_event(21, matched_hash_id="aa", matched_source="guild"))
    assert h.updates[-1][1][0].followups_removed == 2


@pytest.mark.parametrize(
    "kw",
    [
        {},  # no hash match: a fresh risk scan still gets a card
        {"matched_hash_id": "aa", "matched_source": "guild", "uploader_id": 43},
    ],
)
async def test_other_reposts_take_the_normal_path(kw: dict[str, Any]) -> None:
    h = _Harness()
    await h.coord.handle_verdict(_event(3, confirmed_by=77))
    await h.coord.handle_verdict(_event(20, **kw))
    assert len(h.reports) == 2


async def test_global_repost_gets_a_card_once_the_settled_window_passed() -> None:
    # Within the window it is removed quietly (see test_settled_uploader.py).
    h = _Harness()
    await h.coord.handle_verdict(_event(3, confirmed_by=77))
    h.coord._settled.clear()
    await h.coord.handle_verdict(_event(20, matched_hash_id="aa", matched_source="global"))
    assert len(h.reports) == 2


async def test_safe_mode_never_removes_reposts() -> None:
    h = _Harness(safe_mode=True)
    await h.coord.handle_verdict(_event(3, confirmed_by=77))
    await h.coord.handle_verdict(_event(20, matched_hash_id="aa", matched_source="guild"))
    assert "delete_message" not in h.rest.calls
    assert len(h.reports) == 2


async def test_a_refused_delete_still_gets_a_card() -> None:
    class _NoDelete(_FakeRest):
        async def delete_message(self, channel_id: int, message_id: int) -> None:
            raise RuntimeError("Missing Permissions")

    h = _Harness()
    await h.coord.handle_verdict(_event(3, confirmed_by=77))
    h.coord._executor._rest = _NoDelete()  # type: ignore[attr-defined]
    await h.coord.handle_verdict(_event(20, matched_hash_id="aa", matched_source="guild"))
    assert len(h.reports) == 2  # a failure is never silent


async def test_the_campaign_window_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness()
    await h.coord.handle_verdict(_event(3, confirmed_by=77))
    h.coord._campaigns[(1, 42)].expires_at = 0.0
    await h.coord.handle_verdict(_event(20, matched_hash_id="aa", matched_source="guild"))
    assert len(h.reports) == 2
    assert (1, 42) not in h.coord._campaigns


async def test_the_campaign_list_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coordinator_module, "OPEN_CARD_LIMIT", 2)
    h = _Harness()
    for uploader in (1, 2, 3):
        await h.coord.handle_verdict(_event(uploader, uploader_id=uploader, confirmed_by=77))
    assert list(h.coord._campaigns) == [(1, 2), (1, 3)]


def test_the_folded_card_counts_removed_reposts() -> None:
    data = ReportData(
        detection_id=5,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=42,
        verdict="scam",
        confidence=1.0,
        action_taken="report_only",
        decided_by=77,
        followups_removed=3,
    )
    embeds, rows = build_card([data])
    assert rows == []
    assert translate("card.followups_removed", "en", count=3) in embeds[0].description


# --- the closer against a real database --------------------------------------


@pytest_asyncio.fixture
async def scope() -> AsyncIterator[SessionScope]:
    engine: AsyncEngine = create_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)

    @asynccontextmanager
    async def _scope() -> AsyncIterator[_Session]:
        async with session_scope(factory) as s:
            yield s

    yield _scope
    await engine.dispose()


class _DeleteRest:
    def __init__(self, fail: set[int] | None = None) -> None:
        self.deleted: list[tuple[int, int]] = []
        self._fail = fail or set()

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        if message_id in self._fail:
            raise RuntimeError("Unknown Message")
        self.deleted.append((channel_id, message_id))


async def _seed(scope: SessionScope) -> dict[str, int]:
    now = datetime.now(UTC)
    rows = {
        # name: (guild, uploader, message, card, action, reported, age_hours)
        "pressed": (7, 42, 3, 900, "confirmed", True, 0),
        "open_a": (7, 42, 4, 901, "report_only", True, 1),
        "open_a2": (7, 42, 4, 901, "report_only", True, 1),
        "open_b": (7, 42, 5, 902, "delete", True, 2),
        # Settled by the bot itself: a record, never closed or removed.
        "auto_card": (7, 42, 7, 907, "auto:delete_ban", True, 1),
        "auto_followup": (7, 42, 8, 907, "auto:delete", True, 1),
        "no_card_link": (7, 42, 6, None, "report_only", True, 2),
        "dismissed": (7, 42, 10, 903, "dismissed", True, 1),
        "never_reported": (7, 42, 11, None, "none", False, 1),
        "too_old": (7, 42, 12, 904, "report_only", True, 48),
        "other_user": (7, 43, 13, 905, "report_only", True, 1),
        "other_guild": (8, 42, 14, 906, "report_only", True, 1),
    }
    ids: dict[str, int] = {}
    async with scope() as s:
        s.add_all([Guild(guild_id=7), Guild(guild_id=8)])
        await s.flush()
        for n, (name, (guild, uploader, message, card, action, reported, age)) in enumerate(
            rows.items()
        ):
            when = now - timedelta(hours=age)
            det = Detection(
                guild_id=guild,
                channel_id=2,
                message_id=message,
                attachment_id=n,
                uploader_id=uploader,
                distances={},
                verdict="scam",
                idempotency_key=f"seed-{name}",
                action_taken=action,
                review_message_id=card,
                reported_at=when if reported else None,
                created_at=when,
            )
            s.add(det)
            await s.flush()
            ids[name] = det.id
    return ids


async def test_the_closer_closes_only_that_uploaders_open_cards(scope: SessionScope) -> None:
    ids = await _seed(scope)
    rest = _DeleteRest(fail={902})
    coord, _dispatcher = build_coordinator(
        get_settings(),
        scope,
        rest=rest,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        bot_user_id=999,
    )
    assert coord._close_cards is not None
    cleanup = await coord._close_cards(7, 42, 3, 77, 100)

    closed = {"open_a", "open_a2", "open_b", "no_card_link"}
    assert cleanup.closed == len(closed)
    assert cleanup.message_ids == (4, 5, 6)
    assert rest.deleted == [(100, 901)]  # 902 was already gone; 907 is kept
    assert cleanup.cards_deleted == 1

    async with scope() as s:
        actions = {d.id: d.action_taken for d in (await s.execute(select(Detection))).scalars()}
        audit = (await s.execute(select(ModAction))).scalars().one()
    for name, det_id in ids.items():
        if name in closed:
            assert actions[det_id] == "confirmed", name
        elif name != "pressed":
            assert actions[det_id] != "confirmed", name
    assert audit.action == "review.campaign_close"
    assert audit.actor_id == 77
    assert sorted(audit.payload["detections"]) == sorted(ids[n] for n in closed)


async def test_the_closer_without_a_review_channel_still_closes(scope: SessionScope) -> None:
    await _seed(scope)
    rest = _DeleteRest()
    coord, _dispatcher = build_coordinator(
        get_settings(),
        scope,
        rest=rest,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        bot_user_id=999,
    )
    assert coord._close_cards is not None
    cleanup = await coord._close_cards(7, 42, 3, 77, None)
    assert cleanup.closed == 4
    assert rest.deleted == []
    # Nothing left to close a second time: no second audit row.
    again = await coord._close_cards(7, 42, 3, 77, None)
    assert again == CardCleanup()
    async with scope() as s:
        assert len((await s.execute(select(ModAction))).scalars().all()) == 1
