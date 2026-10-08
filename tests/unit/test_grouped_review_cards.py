"""One review card per message, folded once a moderator decides.

A post with several flagged images used to produce one card per image, and a
card kept every button after a decision. Now the images of one message share
one card whose buttons act on all of them, and a decided card collapses to a
single line with no buttons. These tests pin both halves: the card the
coordinator builds and keeps up to date, the buttons acting on the whole
card, ``/queue`` counting cards and reopening a folded one, and the storage
underneath.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.contracts.events import Action, Verdict, VerdictEvent
from optimus.core.config import Settings
from optimus.core.ratelimit import InMemoryRateLimiter
from optimus.db.models import Detection, Guild
from optimus.db.repositories import DetectionRepository, GuildRepository
from optimus.i18n import translate
from optimus.services.interactions.handlers import (
    DetectionFacts,
    InteractionContext,
    handle_command,
)
from optimus.services.interactions.review_buttons import handle_review_button
from optimus.services.interactions.service import DbDeps
from optimus.services.moderation.coordinator import GuildModConfig, ModerationCoordinator
from optimus.shared.outcomes import ActionResult
from optimus.shared.review import (
    MAX_CARD_IMAGES,
    ParsedCustomId,
    ReportData,
    ReviewAction,
    build_card,
    merge_reports,
    report_title,
)
from tests.unit.test_interactions_handlers import ADMIN, FakeDeps

# --- the merged card ----------------------------------------------------------


def _data(det_id: int, **kw: Any) -> ReportData:
    base: dict[str, Any] = {
        "detection_id": det_id,
        "guild_id": 1,
        "channel_id": 2,
        "message_id": 3,
        "uploader_id": 42,
        "verdict": "ambiguous",
        "confidence": 0.5,
        "action_taken": "report_only",
    }
    base.update(kw)
    return ReportData(**base)


def test_a_single_report_is_unchanged() -> None:
    one = _data(5)
    assert merge_reports([one]) is one
    assert report_title(one) == translate("report.title", "en", detection_id=5, verdict="AMBIGUOUS")


def test_merge_keeps_the_strongest_evidence_from_every_image() -> None:
    merged = merge_reports(
        [
            _data(5, image_url="https://cdn/1.png", matched_hash_id="aa", ocr_summary="wallet"),
            _data(
                6,
                verdict="scam",
                confidence=1.0,
                image_url="https://cdn/2.png",
                matched_hash_id="bb",
                ocr_summary="wallet",
            ),
            _data(7, image_url="https://cdn/3.png", matched_hash_id="aa", problem="no access"),
        ]
    )
    assert merged.detection_id == 5  # the card's number and buttons
    assert merged.verdict == "scam"
    assert merged.confidence == 1.0
    assert merged.matched_hash_id == "aa, bb"
    assert merged.ocr_summary == "wallet"
    assert merged.problem == "no access"
    assert merged.image_count == 3
    assert merged.image_url == "https://cdn/1.png"
    assert merged.extra_image_urls == ("https://cdn/2.png", "https://cdn/3.png")
    assert "3" in report_title(merged)


def test_the_gallery_is_capped_at_what_discord_shows() -> None:
    items = [_data(i, image_url=f"https://cdn/{i}.png") for i in range(1, 8)]
    merged = merge_reports(items)
    assert 1 + len(merged.extra_image_urls) == MAX_CARD_IMAGES
    assert merged.image_count == 7


def test_build_card_renders_one_gallery_card_with_buttons() -> None:
    items = [_data(i, image_url=f"https://cdn/{i}.png") for i in (5, 6)]
    embeds, rows = build_card(items)
    assert len(embeds) == 2
    # Discord merges embeds sharing a url into one card with an image grid.
    assert embeds[0].url == embeds[1].url == "https://discord.com/channels/1/2/3"
    assert rows  # undecided: buttons


def test_a_confirmed_card_is_built_folded_without_buttons() -> None:
    embeds, rows = build_card([_data(5, decided_by=77, action_taken="delete_ban")])
    assert rows == []
    (embed,) = embeds
    assert embed.title is None
    assert "<@77>" in embed.description
    assert "delete_ban" in embed.description


def test_a_folded_confirmation_still_spells_out_what_failed() -> None:
    embeds, _rows = build_card(
        [_data(5, decided_by=77, action_taken="delete_ban (failed)", problem="grant Ban Members")]
    )
    assert "grant Ban Members" in embeds[0].description


def test_merge_refuses_an_empty_card() -> None:
    with pytest.raises(ValueError):
        merge_reports([])


# --- the coordinator keeps one card per message -------------------------------


class _Cards:
    """Records card posts/edits/stamps like the review channel would see them."""

    def __init__(self, *, slow: float = 0.0, update_fails: bool = False) -> None:
        self.posted: list[ReportData] = []
        self.updates: list[tuple[int, list[ReportData]]] = []
        self.stamps: list[tuple[int, int | None]] = []
        self._slow = slow
        self._update_fails = update_fails
        self._next = 500

    async def post(self, _channel: int, data: ReportData) -> int | None:
        await asyncio.sleep(self._slow)
        self.posted.append(data)
        self._next += 1
        return self._next

    async def update(self, _channel: int, card_id: int, items: Sequence[ReportData]) -> None:
        if self._update_fails:
            raise RuntimeError("card deleted")
        self.updates.append((card_id, list(items)))

    async def stamp(self, _guild: int, detection_id: int, card_id: int | None) -> None:
        self.stamps.append((detection_id, card_id))


_CFG = GuildModConfig(
    guild_id=1,
    configured_action=Action.REPORT_ONLY,
    mod_queue_threshold=0.5,
    auto_act_threshold=0.9,
    safe_mode=False,
    review_channel_id=10,
)


def _coordinator(cards: _Cards) -> ModerationCoordinator:
    async def unused(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError

    return ModerationCoordinator(
        config=unused,
        target=unused,
        executor=None,  # type: ignore[arg-type]
        report=cards.post,
        audit=unused,
        mark_reported=cards.stamp,
        update_report=cards.update,
    )


def _verdict(attachment_id: int, *, message_id: int = 3, **kw: Any) -> VerdictEvent:
    return VerdictEvent(
        correlation_id="c",
        occurred_at=datetime.now(UTC),
        guild_id=1,
        channel_id=2,
        message_id=message_id,
        attachment_id=attachment_id,
        uploader_id=42,
        idempotency_key=f"k:{message_id}:{attachment_id}",
        verdict=Verdict.SCAM,
        confidence=1.0,
        **kw,
    )


async def _report(coord: ModerationCoordinator, event: VerdictEvent, det_id: int) -> None:
    result = ActionResult(Action.REPORT_ONLY, success=True)
    await coord._post_report(event, _CFG, Action.REPORT_ONLY, det_id, result)


async def test_four_images_finishing_together_share_one_card() -> None:
    cards = _Cards(slow=0.01)
    coord = _coordinator(cards)
    await asyncio.gather(*(_report(coord, _verdict(a), 100 + a) for a in (1, 2, 3, 4)))

    assert len(cards.posted) == 1
    card_id = 501
    assert [len(items) for _id, items in cards.updates] == [2, 3, 4]
    assert all(cid == card_id for cid, _items in cards.updates)
    assert sorted(cards.stamps) == [(101, card_id), (102, card_id), (103, card_id), (104, card_id)]
    assert coord._card_locks == {}  # no lock left behind


async def test_different_messages_get_their_own_cards() -> None:
    cards = _Cards()
    coord = _coordinator(cards)
    await _report(coord, _verdict(1, message_id=3), 101)
    await _report(coord, _verdict(1, message_id=4), 102)
    assert len(cards.posted) == 2
    assert cards.updates == []


async def test_a_redelivered_image_replaces_its_entry_instead_of_duplicating() -> None:
    cards = _Cards()
    coord = _coordinator(cards)
    await _report(coord, _verdict(1), 101)
    await _report(coord, _verdict(2), 102)
    await _report(coord, _verdict(2), 102)
    assert [d.detection_id for d in cards.updates[-1][1]] == [101, 102]


async def test_a_failed_card_update_falls_back_to_a_new_card() -> None:
    cards = _Cards(update_fails=True)
    coord = _coordinator(cards)
    await _report(coord, _verdict(1), 101)
    await _report(coord, _verdict(2), 102)
    assert len(cards.posted) == 2  # never silence


async def test_without_an_updater_every_image_posts_as_before() -> None:
    cards = _Cards()
    coord = _coordinator(cards)
    coord._update_report = None
    await _report(coord, _verdict(1), 101)
    await _report(coord, _verdict(2), 102)
    assert len(cards.posted) == 2


async def test_confirm_writes_its_outcome_onto_the_pressed_card() -> None:
    cards = _Cards()
    coord = _coordinator(cards)
    await _report(coord, _verdict(1), 101)  # the open card, 501
    await _report(coord, _verdict(1, confirmed_by=77, review_card_id=501), 201)
    await _report(coord, _verdict(2, confirmed_by=77, review_card_id=501), 202)

    assert len(cards.posted) == 1  # no second card for the confirmation
    card_id, items = cards.updates[-1]
    assert card_id == 501
    assert [i.detection_id for i in items] == [201, 202]
    assert all(i.decided_by == 77 for i in items)
    assert (202, 501) in cards.stamps
    # The open card is settled: a late image of the message gets a new card
    # rather than turning the folded one back into a full card.
    await _report(coord, _verdict(3), 103)
    assert len(cards.posted) == 2
    assert cards.posted[-1].decided_by is None


async def test_review_as_scam_posts_an_already_folded_card() -> None:
    cards = _Cards()
    coord = _coordinator(cards)
    await _report(coord, _verdict(1, confirmed_by=77), 201)
    await _report(coord, _verdict(2, confirmed_by=77), 202)
    assert len(cards.posted) == 1
    assert cards.posted[0].decided_by == 77
    assert [i.detection_id for i in cards.updates[-1][1]] == [201, 202]


async def test_an_expired_open_card_is_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    from optimus.services.moderation import coordinator as coordinator_module

    cards = _Cards()
    coord = _coordinator(cards)
    await _report(coord, _verdict(1), 101)
    monkeypatch.setattr(coordinator_module, "OPEN_CARD_TTL_SECONDS", -1)
    await _report(coord, _verdict(2), 102)
    assert len(cards.posted) == 2


async def test_remembered_cards_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from optimus.services.moderation import coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "OPEN_CARD_LIMIT", 2)
    cards = _Cards()
    coord = _coordinator(cards)
    for message_id in (3, 4, 5):
        await _report(coord, _verdict(1, message_id=message_id), message_id)
    assert list(coord._open_cards) == [(1, 4, False), (1, 5, False)]


# --- the buttons act on the whole card ----------------------------------------

_HASHES = {"phash": 0xA1, "dhash": 2, "whash": 3, "ahash": 4}


def _group_deps(**flags: Any) -> FakeDeps:
    detections = {
        det_id: DetectionFacts(
            detection_id=det_id,
            channel_id=111,
            message_id=222,
            attachment_id=det_id,
            uploader_id=333,
            hashes={**_HASHES, "phash": 0xA0 + det_id},
        )
        for det_id in (1, 2, 3)
    }
    return FakeDeps(detections=detections, cards={500: [1, 2, 3]}, **flags)


def _press(card: int | None = 500) -> InteractionContext:
    return InteractionContext(
        guild_id=1, user_id=99, member_permissions=ADMIN, command="", card_message_id=card
    )


async def _click(deps: FakeDeps, action: ReviewAction, *, card: int | None = 500) -> Any:
    return await handle_review_button(_press(card), ParsedCustomId(action, 1), deps)


async def test_dismiss_closes_every_image_on_the_card() -> None:
    deps = _group_deps()
    resp = await _click(deps, ReviewAction.DISMISS)
    assert deps.detection_actions == [(1, "dismissed"), (2, "dismissed"), (3, "dismissed")]
    assert resp.card_note_key == "card.handled"


async def test_confirm_deletes_once_and_blocklists_every_image() -> None:
    deps = _group_deps()
    resp = await _click(deps, ReviewAction.CONFIRM_SCAM)
    assert deps.deleted_messages == [(111, 222)]
    assert sorted(deps.hashes) == [f"{0xA0 + i:016x}" for i in (1, 2, 3)]
    assert [c["attachment_id"] for c in deps.confirmed_scams] == [1, 2, 3]
    assert deps.confirm_meta == [{"confirmed_by": 99, "review_card_id": 500}] * 3
    assert {a for _d, a in deps.detection_actions} == {"confirmed"}
    assert resp.card_note_key == "card.handled"


async def test_false_positive_whitelists_every_image_and_unbans_once() -> None:
    deps = _group_deps()
    await _click(deps, ReviewAction.FALSE_POSITIVE)
    assert len(deps.whitelisted) == 3
    assert deps.unbans == [(1, 333)]
    assert deps.reversed == [1, 2, 3]


async def test_whitelist_and_ban_cover_the_card() -> None:
    deps = _group_deps()
    await _click(deps, ReviewAction.WHITELIST_IMAGE)
    assert len(deps.whitelisted) == 3
    await _click(deps, ReviewAction.BAN_UPLOADER)
    assert len(deps.bans) == 1
    assert [a for _d, a in deps.detection_actions] == ["banned"] * 3


async def test_a_refused_ban_folds_nothing() -> None:
    deps = _group_deps(rest_ban_ok=False)
    resp = await _click(deps, ReviewAction.BAN_UPLOADER)
    assert resp.i18n_key == "button.action_failed"
    assert resp.card_note_key is None


async def test_an_old_card_without_links_acts_on_its_own_detection_only() -> None:
    deps = _group_deps()
    await _click(deps, ReviewAction.DISMISS, card=None)
    assert deps.detection_actions == [(1, "dismissed")]
    await _click(deps, ReviewAction.DISMISS, card=777)  # card with no linked rows
    assert deps.detection_actions[-1] == (1, "dismissed")
    assert len(deps.detection_actions) == 2


async def test_a_forged_button_cannot_act_on_another_card() -> None:
    """The button's own detection must be on the card the press came from."""
    deps = _group_deps()
    deps.cards[600] = [2, 3]
    await handle_review_button(_press(600), ParsedCustomId(ReviewAction.DISMISS, 1), deps)
    assert deps.detection_actions == [(1, "dismissed")]


async def test_a_repeated_image_is_hashed_once() -> None:
    """A confirmed verdict records a second row for the same attachment."""
    deps = _group_deps()
    deps.detections[4] = DetectionFacts(
        detection_id=4,
        channel_id=111,
        message_id=222,
        attachment_id=1,
        uploader_id=333,
        hashes={**_HASHES, "phash": 0xA1},
    )
    deps.cards[500] = [1, 2, 3, 4]
    await _click(deps, ReviewAction.WHITELIST_IMAGE)
    assert len(deps.whitelisted) == 3


# --- /queue counts cards and reopens a folded one ------------------------------


def _queue_ctx(**opts: Any) -> InteractionContext:
    return InteractionContext(
        guild_id=1, user_id=99, member_permissions=ADMIN, command="queue", options=opts
    )


async def test_queue_detection_reopens_every_image_of_the_message() -> None:
    deps = _group_deps()
    resp = await handle_command(_queue_ctx(detection=2), deps)
    assert resp.i18n_key == "command.queue_reopened"
    assert deps.reposted == [[1, 2, 3]]
    assert ("review.reopen", "2") in [(a[2], a[3]) for a in deps.audits]


async def test_queue_detection_unknown_or_unpostable() -> None:
    missing = FakeDeps(detection_missing=True)
    resp = await handle_command(_queue_ctx(detection=9), missing)
    assert resp.i18n_key == "button.detection_missing"

    failing = _group_deps(repost_fails=True)
    resp = await handle_command(_queue_ctx(detection=1), failing)
    assert resp.i18n_key == "command.queue_reopen_failed"


async def test_queue_lists_a_multi_image_card_once_with_its_count() -> None:
    row = {
        "detection_id": 1,
        "channel_id": 2,
        "message_id": 3,
        "uploader_id": 4,
        "verdict": "scam",
        "images": 4,
        "age_seconds": 60.0,
    }
    deps = FakeDeps(queue={"total": 1, "rows": [row]})
    resp = await handle_command(_queue_ctx(), deps)
    assert ", 4 images" in resp.params["listing"]


# --- storage -------------------------------------------------------------------


async def _guild(session: AsyncSession, guild_id: int, **kw: Any) -> None:
    await GuildRepository(session).upsert(Guild(guild_id=guild_id, **kw))


async def _det(repo: DetectionRepository, message_id: int, attachment_id: int) -> Detection:
    return await repo.record(
        Detection(
            message_id=message_id,
            channel_id=11,
            attachment_id=attachment_id,
            uploader_id=13,
            distances={},
            verdict="scam",
            idempotency_key=f"{repo._guild_id}:{message_id}:{attachment_id}",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )


async def test_card_links_and_the_grouped_queue(session: AsyncSession) -> None:
    await _guild(session, 5)
    await _guild(session, 6)
    repo = DetectionRepository(session, guild_id=5)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    a = await _det(repo, 100, 1)
    b = await _det(repo, 100, 2)
    c = await _det(repo, 101, 1)
    for det in (a, b):
        await repo.set_reported_at(det.id, when, review_message_id=900)
    await repo.set_reported_at(c.id, when)

    assert [d.id for d in await repo.list_on_card(900)] == [a.id, b.id]
    assert await DetectionRepository(session, guild_id=6).list_on_card(900) == []
    assert [d.id for d in await repo.list_for_message(100)] == [a.id, b.id]

    rows, total = await repo.list_open(limit=25)
    assert total == 2  # two cards waiting, not three images
    by_message = {r["message_id"]: r for r in rows}
    assert by_message[100]["images"] == 2
    assert by_message[100]["detection_id"] == a.id
    assert by_message[101]["images"] == 1

    # Stamping without a card id leaves an existing link alone.
    await repo.set_reported_at(a.id, when)
    assert [d.id for d in await repo.list_on_card(900)] == [a.id, b.id]

    assert await repo.link_to_card([a.id, b.id, c.id], 901) == 3
    assert [d.id for d in await repo.list_on_card(901)] == [a.id, b.id, c.id]
    assert await repo.link_to_card([], 902) == 0


class _CardRest:
    def __init__(self, fail: bool = False) -> None:
        self.cards: list[tuple[int, list[ReportData]]] = []
        self._fail = fail

    async def post_review_card(self, channel_id: int, items: Sequence[ReportData]) -> int:
        if self._fail:
            raise RuntimeError("no access")
        self.cards.append((channel_id, list(items)))
        return 4242


def _db_deps(session: AsyncSession, rest: object | None) -> DbDeps:
    return DbDeps(session, InMemoryRateLimiter(), Settings(), rest=rest)  # type: ignore[arg-type]


async def test_repost_shows_each_image_once_and_links_every_row(session: AsyncSession) -> None:
    await _guild(session, 5, review_channel_id=77)
    repo = DetectionRepository(session, guild_id=5)
    first = await _det(repo, 100, 1)
    second = await _det(repo, 100, 2)
    # The confirmed verdict's own row for attachment 1.
    again = await repo.record(
        Detection(
            message_id=100,
            channel_id=11,
            attachment_id=1,
            uploader_id=13,
            distances={},
            verdict="scam",
            idempotency_key="reviewmsg:5:100:1",
        )
    )
    rest = _CardRest()
    deps = _db_deps(session, rest)
    group = await deps.get_message_detections(5, 100)
    assert await deps.repost_review_card(5, group) == 4242

    ((channel_id, items),) = rest.cards
    assert channel_id == 77
    assert [i.detection_id for i in items] == [first.id, second.id]
    linked = await deps.get_card_detections(5, 4242)
    assert [d.detection_id for d in linked] == [first.id, second.id, again.id]


async def test_repost_needs_a_channel_rest_and_a_working_post(session: AsyncSession) -> None:
    await _guild(session, 5)
    repo = DetectionRepository(session, guild_id=5)
    det = await _det(repo, 100, 1)
    facts = await _db_deps(session, None).get_message_detections(5, 100)
    assert await _db_deps(session, None).repost_review_card(5, facts) is None
    assert await _db_deps(session, _CardRest()).repost_review_card(5, facts) is None  # no channel
    assert await _db_deps(session, _CardRest()).repost_review_card(5, []) is None

    await GuildRepository(session).upsert(Guild(guild_id=5, review_channel_id=77))
    failing = _db_deps(session, _CardRest(fail=True))
    assert await failing.repost_review_card(5, facts) is None
    assert await repo.list_on_card(4242) == []
    assert det.review_message_id is None
