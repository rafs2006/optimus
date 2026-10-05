"""Uploaders who already left are still banned, and fully handled cards close.

Scam accounts often post and leave within seconds. Discord bans by user id,
members or not, so a ``delete_ban`` policy still bans them; without that they
simply rejoin. A match against this server's own blocklist that the bot
handled in full is posted as a folded card that keeps only False positive, and
it settles the uploader's other open cards just like a moderator's Confirm.
Anything a person should look at keeps the full, open card.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import fakeredis.aioredis

from optimus.contracts.events import Action, Verdict, VerdictEvent
from optimus.i18n import translate
from optimus.services.moderation.boundaries import TargetContext
from optimus.services.moderation.coordinator import CardCleanup, ModerationCoordinator
from optimus.services.moderation.review import (
    AUTO_HANDLED_BUTTONS,
    ReportData,
    ReviewAction,
    build_card,
    decided_note,
    merge_reports,
)
from optimus.services.moderation.sweep import SweepOutcome
from tests.unit.test_coordinator import _build, _cfg, _FakeRest, _target


class _DiscordError(Exception):
    def __init__(self, code: int, status: int = 400) -> None:
        super().__init__(f"discord error {code}")
        self.code = code
        self.status = status


def _departed() -> TargetContext:
    return TargetContext(
        user_id=42,
        guild_owner_id=0,
        bot_user_id=999,
        is_administrator=False,
        top_role_position=0,
        bot_top_role_position=0,
        in_guild=False,
    )


def _event(message_id: int = 3, attachment_id: int = 1, **kw: Any) -> VerdictEvent:
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
        "confidence": 0.97,
        "matched_source": "guild",
        "matched_hash_id": "abcd",
    }
    base.update(kw)
    return VerdictEvent(**base)


class _Harness:
    def __init__(
        self, *, target: TargetContext | None, action: Action = Action.DELETE_BAN, **cfg: Any
    ) -> None:
        self.rest = _FakeRest()
        self.reports: list[ReportData] = []
        self.audits: list[tuple[str, bool]] = []
        self.closes: list[tuple[Any, ...]] = []
        self.updates: list[tuple[int, list[ReportData]]] = []

        async def sweep(_event: VerdictEvent) -> SweepOutcome:
            return SweepOutcome(deleted=1, channels=1)

        self.coord: ModerationCoordinator = _build(
            rest=self.rest,
            redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
            cfg=_cfg(configured_action=action, **cfg),
            target=target,
            reports=self.reports,
            audits=self.audits,
            sweep=sweep,
        )
        self.coord._close_cards = self._close
        self.coord._update_report = self._update
        self.coord._audit = self._audit

    async def _audit(self, _event: VerdictEvent, action: str, result: Any) -> int:
        self.audits.append((action, result.success))
        return 100 + len(self.audits)

    async def _close(self, *args: Any) -> CardCleanup:
        self.closes.append(args)
        return CardCleanup(closed=4, cards_deleted=4, message_ids=(8,))

    async def _update(self, _channel: int, card_id: int, items: Sequence[ReportData]) -> None:
        self.updates.append((card_id, list(items)))


# --- Banning an uploader who already left ----------------------------------


async def test_departed_uploader_is_banned_by_id_and_the_card_closes() -> None:
    h = _Harness(target=_departed())
    result = await h.coord.handle_verdict(_event())

    assert result.action is Action.DELETE_BAN
    assert result.success
    assert "delete_message" in h.rest.calls
    assert "ban_member" in h.rest.calls
    (card,) = h.reports
    assert card.auto_handled
    assert card.decided_by is None
    assert translate("report.boundary_departed_banned", "en") in card.action_taken
    # The bot settles the campaign itself, under the system actor.
    assert h.closes == [(1, 42, 3, 0, 100)]
    assert "cleared 4 other report(s) from this uploader" in card.action_taken


async def test_departed_uploader_already_banned_counts_as_handled() -> None:
    h = _Harness(target=_departed())

    async def _already(*_a: Any, **_k: Any) -> None:
        raise _DiscordError(40007)

    h.rest.ban_member = _already  # type: ignore[method-assign]
    result = await h.coord.handle_verdict(_event())
    assert result.success
    assert h.reports[0].auto_handled


async def test_ban_limit_keeps_the_card_open_with_the_reason() -> None:
    h = _Harness(target=_departed())

    async def _limit(*_a: Any, **_k: Any) -> None:
        raise _DiscordError(30035)

    h.rest.ban_member = _limit  # type: ignore[method-assign]
    result = await h.coord.handle_verdict(_event())
    assert not result.success
    (card,) = h.reports
    assert not card.auto_handled
    assert card.problem is not None
    assert h.closes == []


async def test_unknown_user_keeps_the_card_open_with_the_reason() -> None:
    h = _Harness(target=_departed())

    async def _unknown(*_a: Any, **_k: Any) -> None:
        raise _DiscordError(10013, status=404)

    h.rest.ban_member = _unknown  # type: ignore[method-assign]
    result = await h.coord.handle_verdict(_event())
    assert not result.success
    (card,) = h.reports
    assert not card.auto_handled
    assert card.problem is not None


async def test_departed_uploader_under_kick_policy_is_delete_only() -> None:
    h = _Harness(target=_departed(), action=Action.DELETE_KICK)
    result = await h.coord.handle_verdict(_event())

    assert result.action is Action.DELETE
    assert "kick_member" not in h.rest.calls
    (card,) = h.reports
    assert not card.auto_handled
    note = translate("report.boundary_departed", "en", action="delete_kick")
    assert note in card.action_taken


async def test_unverifiable_uploader_is_never_punished_blind() -> None:
    h = _Harness(target=None)
    result = await h.coord.handle_verdict(_event())

    assert result.action is Action.DELETE
    assert "ban_member" not in h.rest.calls
    (card,) = h.reports
    assert not card.auto_handled
    note = translate("report.boundary_unverified", "en", action="delete_ban")
    assert note in card.action_taken


async def test_global_match_never_bans_even_when_the_uploader_left() -> None:
    h = _Harness(target=_departed())
    result = await h.coord.handle_verdict(_event(matched_source="global"))

    assert result.action is Action.REPORT_ONLY
    assert "ban_member" not in h.rest.calls
    assert "delete_message" not in h.rest.calls
    assert not h.reports[0].auto_handled


async def test_confirm_bans_a_departed_uploader_and_stays_a_moderator_card() -> None:
    h = _Harness(target=_departed())
    result = await h.coord.handle_verdict(_event(confirmed_by=77, confidence=1.0))

    assert result.action is Action.DELETE_BAN
    assert "ban_member" in h.rest.calls
    (card,) = h.reports
    assert card.decided_by == 77
    assert not card.auto_handled
    assert h.closes == [(1, 42, 3, 77, 100)]


# --- When a card closes by itself, and when it stays open ------------------


async def test_member_fully_handled_closes_the_card() -> None:
    h = _Harness(target=_target())
    await h.coord.handle_verdict(_event())
    (card,) = h.reports
    assert card.auto_handled
    assert "report.boundary" not in card.action_taken


async def test_delete_only_policy_fully_handled_closes_the_card() -> None:
    h = _Harness(target=_target(), action=Action.DELETE)
    await h.coord.handle_verdict(_event())
    assert h.reports[0].auto_handled


async def test_refused_delete_keeps_the_card_open() -> None:
    h = _Harness(target=_departed())

    async def _forbidden(*_a: Any, **_k: Any) -> None:
        raise _DiscordError(50013, status=403)

    h.rest.delete_message = _forbidden  # type: ignore[method-assign]
    await h.coord.handle_verdict(_event())
    assert not h.reports[0].auto_handled
    assert h.closes == []


async def test_near_match_queued_for_review_keeps_the_card_open() -> None:
    h = _Harness(target=_departed())
    result = await h.coord.handle_verdict(_event(confidence=0.6))
    assert result.action is Action.REPORT_ONLY
    assert not h.reports[0].auto_handled


async def test_safe_mode_keeps_the_card_open() -> None:
    h = _Harness(target=_departed(), safe_mode=True)
    await h.coord.handle_verdict(_event())
    assert "ban_member" not in h.rest.calls
    assert not h.reports[0].auto_handled


async def test_role_hierarchy_refusal_keeps_the_card_open() -> None:
    h = _Harness(target=_target(top_role_position=9))
    result = await h.coord.handle_verdict(_event())
    assert result.action is Action.REPORT_ONLY
    assert not h.reports[0].auto_handled


async def test_member_report_keeps_the_card_open() -> None:
    h = _Harness(target=_target())
    await h.coord.handle_verdict(_event(reported_by=55))
    assert not h.reports[0].auto_handled


async def test_match_without_a_hash_keeps_the_card_open() -> None:
    h = _Harness(target=_target())
    await h.coord.handle_verdict(_event(matched_hash_id=None))
    assert not h.reports[0].auto_handled


async def test_later_images_join_the_folded_card() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(attachment_id=1))
    await h.coord.handle_verdict(_event(attachment_id=2))
    assert len(h.reports) == 1  # one card for the post
    ((_card_id, items),) = h.updates
    assert len(items) == 2
    assert all(i.auto_handled for i in items)


async def test_auto_handled_uploader_reposts_are_removed_quietly() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    await h.coord.handle_verdict(_event(message_id=11))
    assert len(h.reports) == 1  # the repost got no card of its own
    assert h.updates[-1][1][0].followups_removed == 1


# --- Rendering --------------------------------------------------------------


def _data(**kw: Any) -> ReportData:
    base: dict[str, Any] = {
        "detection_id": 9,
        "guild_id": 1,
        "channel_id": 2,
        "message_id": 3,
        "uploader_id": 42,
        "verdict": "scam",
        "confidence": 0.97,
        "action_taken": "delete_ban",
    }
    base.update(kw)
    return ReportData(**base)


def test_auto_handled_card_is_folded_with_only_false_positive() -> None:
    embeds, rows = build_card([_data(auto_handled=True)])
    assert len(embeds) == 1
    assert translate("card.handled_auto", "en") in embeds[0].description
    ids = [c.custom_id for row in rows for c in row.components]  # type: ignore[attr-defined]
    assert ids == [f"om:v1:{a.value}:9" for a in AUTO_HANDLED_BUTTONS]
    assert AUTO_HANDLED_BUTTONS == (ReviewAction.FALSE_POSITIVE,)


def test_open_card_keeps_every_button() -> None:
    _embeds, rows = build_card([_data()])
    ids = [c.custom_id for row in rows for c in row.components]  # type: ignore[attr-defined]
    assert f"om:v1:{ReviewAction.CONFIRM_SCAM.value}:9" in ids


def test_moderator_decision_wins_over_auto_on_the_note() -> None:
    note = decided_note(_data(auto_handled=True, decided_by=77))
    assert "<@77>" in note
    embeds, rows = build_card([_data(auto_handled=True, decided_by=77)])
    assert rows == []
    assert len(embeds) == 1


def test_merge_keeps_auto_handled() -> None:
    merged = merge_reports([_data(), _data(detection_id=10, auto_handled=True)])
    assert merged.auto_handled


def test_serbian_auto_note_is_translated() -> None:
    assert translate("card.handled_auto", "sr") != translate("card.handled_auto", "en")
