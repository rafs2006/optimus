"""Near matches spread across channels are acted on without a moderator.

Reported case (#1561): one account posted the same 4 scam images in 8 posts
across 7 channels within 20 seconds. Every image matched this server's list,
but only as a near match (0.57 to 0.77, under the 0.85 bar), so each image
waited for a moderator: 32 open reports for one obvious campaign.

Spreading a match across channels is the scam pattern itself. Once one
uploader's near matches reach ``spread_channels`` distinct channels inside the
window, the server's action_policy runs as if the match were strong. The match
confidence is not changed; the card says why the bot acted.
"""

from __future__ import annotations

from typing import Any

from optimus.contracts.events import Action
from optimus.i18n import translate
from optimus.services.moderation.review import merge_reports
from tests.unit.test_coordinator import _target
from tests.unit.test_departed_ban_auto_close import _departed, _event, _Harness


def _h(**kw: Any) -> _Harness:
    kw.setdefault("target", _departed())
    kw.setdefault("spread_channels", 3)
    return _Harness(**kw)


def _post(message_id: int, channel_id: int, **kw: Any) -> list:
    return [
        _event(message_id=message_id, attachment_id=a, channel_id=channel_id, confidence=c, **kw)
        for a, c in enumerate((0.77, 0.57, 0.73, 0.57))
    ]


async def _run(h: _Harness, posts: list[list]) -> None:
    for post in posts:
        for e in post:
            await h.coord.handle_verdict(e)


async def test_the_third_channel_bans_and_settles_the_campaign() -> None:
    h = _h()
    await _run(h, [_post(10, 201), _post(11, 202), _post(12, 203)])

    assert h.rest.calls.count("ban_member") == 1
    # Posts in channels 201 and 202 asked a moderator; the third settles them.
    assert h.closes == [(1, 42, 12, 0, 100)]
    card = h.reports[-1]
    assert card.message_id == 12
    assert card.auto_handled
    note = translate("report.spread_channels", "en", channels=3, minutes=10)
    assert note in card.action_taken
    assert card.confidence == 0.77  # the score is reported as it was


async def test_later_posts_are_removed_without_cards() -> None:
    h = _h()
    # One image per post keeps the test's small action budget out of the way.
    await _run(h, [_post(m, 200 + m)[:1] for m in range(10, 18)])

    assert h.rest.calls.count("ban_member") == 1
    assert len(h.reports) == 3  # two open cards (closed) and the settled one
    _card_id, items = h.updates[-1]
    assert merge_reports(items).followups_removed == 5


async def test_a_settled_uploader_is_never_escalated_again() -> None:
    h = _h()
    await _run(h, [_post(10, 201)[:1], _post(11, 202)[:1], _post(12, 203)[:1]])

    async def _refuse(*_a: Any, **_k: Any) -> None:
        raise PermissionError("missing access")

    h.rest.delete_message = _refuse  # type: ignore[method-assign]
    await _run(h, [_post(13, 204)[:1]])
    assert h.rest.calls.count("ban_member") == 1
    assert not h.reports[-1].auto_handled  # the refused delete stays visible


async def test_one_channel_never_triggers() -> None:
    h = _h()
    await _run(h, [_post(m, 201) for m in range(10, 15)])
    assert "ban_member" not in h.rest.calls
    assert not any(r.auto_handled for r in h.reports)


async def test_global_matches_and_member_reports_do_not_count() -> None:
    h = _h()
    await _run(h, [_post(m, 200 + m, matched_source="global") for m in range(10, 14)])
    await _run(h, [_post(m, 300 + m, reported_by=7) for m in range(20, 24)])
    assert "ban_member" not in h.rest.calls
    assert "delete_message" not in h.rest.calls


async def test_safe_mode_and_report_only_never_trigger() -> None:
    for kw in ({"safe_mode": True}, {"action": Action.REPORT_ONLY}):
        h = _h(**kw)
        await _run(h, [_post(m, 200 + m) for m in range(10, 14)])
        assert "ban_member" not in h.rest.calls
        assert "delete_message" not in h.rest.calls


async def test_off_when_set_to_zero() -> None:
    h = _h(spread_channels=0)
    await _run(h, [_post(m, 200 + m) for m in range(10, 17)])
    assert "ban_member" not in h.rest.calls


async def test_channels_outside_the_window_do_not_count() -> None:
    h = _h(spread_window_seconds=60)
    await _run(h, [_post(10, 201), _post(11, 202)])
    for seen in h.coord._spread.values():
        for channel in seen:
            seen[channel] -= 120  # both posts are now two minutes old
    await _run(h, [_post(12, 203)])
    assert "ban_member" not in h.rest.calls


async def test_a_moderator_posting_around_is_not_banned() -> None:
    h = _h(target=_target(is_administrator=True))
    await _run(h, [_post(m, 200 + m) for m in range(10, 14)])
    assert "ban_member" not in h.rest.calls
