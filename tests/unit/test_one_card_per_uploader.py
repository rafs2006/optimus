"""A burst from one uploader leaves one card, whatever the number of posts.

Reported case: a scam account posted 4 images in one channel and 4 in another
two seconds apart, all on this server's own list. The verdicts ran side by
side: several images banned at once (two hit Discord's rate limit and came out
as open cards), each post got cards of its own, and each settlement removed
the other's cards -- the review channel ended up with nothing.

Now verdicts run one at a time per uploader. The first image bans and posts
the card; the other images of that post join it, and the other post is
deleted quietly and counted on it. The bot's own cards are stored as settled,
so no cleanup removes them.
"""

from __future__ import annotations

import asyncio

from optimus.services.moderation.review import (
    _merge_actions,
    merge_reports,
    stored_action_label,
)
from optimus.services.moderation.sweep import SweepOutcome
from tests.unit.test_departed_ban_auto_close import _departed, _event, _Harness


def _burst() -> list:
    first = [_event(message_id=3, attachment_id=a, confidence=0.86 + a / 100) for a in range(4)]
    second = [_event(message_id=11, attachment_id=a, confidence=0.86 + a / 100) for a in range(4)]
    return first + second


async def test_a_two_post_burst_leaves_one_card_and_one_ban() -> None:
    h = _Harness(target=_departed())
    await asyncio.gather(*(h.coord.handle_verdict(e) for e in _burst()))

    assert len(h.reports) == 1  # one card for the whole burst
    assert h.rest.calls.count("ban_member") == 1  # one ban, no rate-limited retries
    card_id, items = h.updates[-1]
    assert card_id == 7
    assert len(items) == 4  # the first post's four images on that card
    assert items[0].followups_removed == 1  # the second post counts once, not per image
    assert all(i.auto_handled for i in items)


async def test_the_bots_own_rows_are_stored_as_settled() -> None:
    h = _Harness(target=_departed())
    await asyncio.gather(*(h.coord.handle_verdict(e) for e in _burst()))
    actions = [a for a, _ok in h.audits]
    assert actions[0] == "auto:delete_ban"
    assert set(actions[1:]) == {"auto:delete"}
    assert len(actions) == 8  # every image is still on record


async def test_no_cleanup_runs_with_nothing_kept() -> None:
    h = _Harness(target=_departed())
    await asyncio.gather(*(h.coord.handle_verdict(e) for e in _burst()))
    # Only the first image's settlement closes cards, keeping its own post.
    assert h.closes == [(1, 42, 3, 0, 100)]


async def test_other_uploaders_are_not_held_up() -> None:
    h = _Harness(target=_departed())
    other = _event(message_id=20, uploader_id=43)
    await asyncio.gather(h.coord.handle_verdict(_event()), h.coord.handle_verdict(other))
    assert len(h.reports) == 2
    assert not h.coord._uploader_locks  # lock entries are dropped when idle


async def test_joined_images_do_not_repeat_the_cleanup_counts() -> None:
    # Non-zero sweep and cleanup, as in production: the merged line must show
    # one sweep and one cleanup, not one per image on the card.
    h = _Harness(target=_departed())

    async def sweep(_event: object) -> SweepOutcome:
        return SweepOutcome(deleted=3, channels=2, harvested=("h1",))

    h.coord._sweep = sweep
    await asyncio.gather(*(h.coord.handle_verdict(e) for e in _burst()))
    _card_id, items = h.updates[-1]
    merged = merge_reports(items)
    assert merged.action_taken == (
        "delete_ban — banned by user ID: the uploader had already left"
        " — purged 3 more in 2 channels, +1 hashes blocklisted"
        " — cleared 4 other report(s) from this uploader"
    )
    joined = items[1:]
    assert all(i.action_taken == "delete" for i in joined)
    assert all(i.image_url is None and i.evidence_url is None for i in joined)


def test_cleared_notes_add_up_on_one_card() -> None:
    merged = _merge_actions(
        [
            "delete_ban — cleared 1 other report(s) from this uploader",
            "delete_ban — cleared 2 other report(s) from this uploader",
        ]
    )
    assert merged == "delete_ban — cleared 3 other report(s) from this uploader"


def test_a_reopened_card_says_the_bot_handled_it() -> None:
    assert stored_action_label("auto:delete_ban") == "delete_ban (handled automatically)"
    assert stored_action_label("confirmed") == "confirmed"


def test_a_plain_delete_is_implied_by_delete_ban() -> None:
    assert _merge_actions(["delete_ban — x", "delete"]) == "delete_ban — x"
    assert _merge_actions(["delete", "report_only"]) == "delete; report_only"
