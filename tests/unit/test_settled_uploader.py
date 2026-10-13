"""Once an uploader is settled here, their other matches need no moderator.

A scam account often posts twice within seconds. The first post handled in
full (or confirmed by a moderator) settles the uploader: banned on this
server's own list. For the next ten minutes any other hash match of theirs --
a near match, or a global-list match that on its own only asks a moderator --
is deleted quietly and counted on the settled card. A post checked at the same
moment, whose open card lands after the settlement's cleanup ran, is closed
the same way instead of waiting for a moderator.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
from structlog.testing import capture_logs

from optimus.contracts.events import Action, Verdict, VerdictEvent
from optimus.i18n import translate
from optimus.services.moderation import coordinator as coordinator_module
from optimus.services.moderation.boundaries import TargetContext
from optimus.shared.review import merge_reports
from tests.unit.test_departed_ban_auto_close import _data, _departed, _event, _Harness

# --- Within the settled window ----------------------------------------------


async def test_global_match_from_a_settled_uploader_is_removed_quietly() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    result = await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))

    assert result.action is Action.DELETE
    assert len(h.reports) == 1  # no open card for the global match
    assert h.updates[-1][1][0].followups_removed == 1


def _risk_scan(**kw: object) -> VerdictEvent:
    """An image only the OCR/QR risk scan flagged: no hash match at all."""
    base: dict[str, object] = {
        "verdict": Verdict.AMBIGUOUS,
        "confidence": 0.9,
        "matched_hash_id": None,
        "matched_source": None,
    }
    base.update(kw)
    return _event(**base)


async def test_post_flagged_only_by_the_risk_scan_after_a_ban_is_removed_quietly() -> None:
    # Detection #2288: three images of a post matched the list and one banned
    # the uploader; the fourth image, which only the risk scan flagged, then
    # opened a card for a moderator to confirm a scam already dealt with.
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    with capture_logs() as logs:
        result = await h.coord.handle_verdict(_risk_scan(message_id=3, attachment_id=4))
        await h.coord.handle_verdict(_risk_scan(message_id=11))

    assert result.action is Action.DELETE and result.success
    assert len(h.reports) == 1  # only the settled card
    assert h.rest.calls.count("ban_member") <= 1  # never a second ban
    lines = [e for e in logs if e["event"] == "followup_removed"]
    assert [(e["message_id"], e["path"], e["matched"]) for e in lines] == [
        (3, "banned", False),
        (11, "banned", False),
    ]


async def test_member_report_on_a_settled_uploader_is_cleaned_up() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    result = await h.coord.handle_verdict(_risk_scan(message_id=11, reported_by=5, confidence=1.0))
    assert result.action is Action.DELETE and result.success
    assert len(h.reports) == 1
    assert h.updates[-1][1][0].followups_removed == 1


async def test_near_match_from_a_settled_uploader_is_removed_quietly() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    await h.coord.handle_verdict(_event(message_id=11, confidence=0.6))
    assert len(h.reports) == 1


async def test_a_confirm_settles_the_uploader_too() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3, confirmed_by=77, confidence=1.0))
    await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))
    assert len(h.reports) == 1


async def test_global_match_after_the_window_keeps_its_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    key = (1, 42)
    h.coord._settled[key] = time.monotonic() - coordinator_module.SETTLED_WINDOW_SECONDS - 1
    result = await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))

    assert result.action is Action.REPORT_ONLY
    assert len(h.reports) == 2
    assert not h.reports[1].auto_handled


async def test_global_match_from_an_unsettled_uploader_still_only_asks() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(matched_source="global"))
    assert "delete_message" not in h.rest.calls
    assert h.closes == []


# --- The race: settled while the other post was being checked --------------


async def test_open_card_that_lands_after_the_settlement_is_closed() -> None:
    h = _Harness(target=_departed())
    # The other post settled the uploader, but its card (and so the campaign)
    # is not posted yet -- exactly the 3-second gap from the report.
    h.coord._mark_settled(_event(message_id=3))
    await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))

    assert len(h.reports) == 1  # the open card was posted ...
    assert "delete_message" in h.rest.calls  # ... the post deleted ...
    # ... and the card closed and removed, under the system actor; 0 keeps none.
    assert h.closes == [(1, 42, 0, 0, 100)]
    assert "ban_member" not in h.rest.calls  # the ban still rests on the own list


async def test_late_card_with_a_refused_delete_stays_open() -> None:
    from tests.unit.test_departed_ban_auto_close import _DiscordError

    h = _Harness(target=_departed())

    async def _forbidden(*_a: object, **_k: object) -> None:
        raise _DiscordError(50013, status=403)

    h.rest.delete_message = _forbidden  # type: ignore[method-assign]
    h.coord._mark_settled(_event(message_id=3))
    await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))
    assert h.closes == []


async def test_member_report_card_that_lands_after_the_settlement_is_closed() -> None:
    h = _Harness(target=_departed())
    h.coord._mark_settled(_event(message_id=3), banned=True)
    await h.coord.handle_verdict(_risk_scan(message_id=11, reported_by=5, confidence=1.0))
    assert "delete_message" in h.rest.calls
    assert h.closes == [(1, 42, 0, 0, 100)]
    assert "ban_member" not in h.rest.calls


async def test_risk_scan_card_that_lands_after_the_settlement_is_closed() -> None:
    h = _Harness(target=_departed())
    h.coord._mark_settled(_event(message_id=3), banned=True)
    await h.coord.handle_verdict(_risk_scan(message_id=11))
    assert h.closes == [(1, 42, 0, 0, 100)]


async def test_clean_image_from_a_settled_uploader_is_left_alone() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    h.rest.calls.clear()
    await h.coord.handle_verdict(
        _event(message_id=11, verdict=Verdict.CLEAN, matched_hash_id=None, matched_source=None)
    )
    assert "delete_message" not in h.rest.calls


async def test_safe_mode_never_deletes_for_a_settled_uploader() -> None:
    h = _Harness(target=_departed(), safe_mode=True)
    h.coord._mark_settled(_event(message_id=3))
    await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))
    assert "delete_message" not in h.rest.calls
    assert h.closes == []


# --- Logging and the card's cleanup note ------------------------------------


async def test_every_image_logs_its_decision() -> None:
    h = _Harness(target=_departed())
    with capture_logs() as logs:
        await h.coord.handle_verdict(_event(confidence=0.6, matched_source="global"))
    (line,) = [entry for entry in logs if entry["event"] == "verdict_decided"]
    assert line["decision"] == "mod_queue"
    assert line["reason"] == "global_match_review_only"
    assert line["confidence"] == 0.6
    assert line["matched_source"] == "global"


def test_cleanup_notes_add_up_on_a_multi_image_card() -> None:
    merged = merge_reports(
        [
            _data(
                detection_id=9,
                action_taken="delete_ban — purged 1 more in 1 channels, +1 hashes blocklisted"
                " — cleared 4 other report(s) from this uploader",
            ),
            _data(detection_id=10, action_taken="delete_ban — purged 1 more in 1 channels"),
        ]
    )
    assert merged.action_taken == (
        "delete_ban — purged 2 more in 1 channels, +1 hashes blocklisted"
        " — cleared 4 other report(s) from this uploader"
    )


# --- "Already banned" instead of "had already left" -------------------------


def _banned_already() -> TargetContext:
    return replace(_departed(), already_banned=True)


async def test_confirm_after_the_bots_own_ban_says_already_banned() -> None:
    h = _Harness(target=_banned_already())
    await h.coord.handle_verdict(_event(message_id=3, confirmed_by=77, confidence=1.0))
    note = h.reports[-1].action_taken
    assert translate("report.boundary_already_banned", "en") in note
    assert translate("report.boundary_departed_banned", "en") not in note


async def test_an_uploader_who_really_left_still_reads_departed() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3, confirmed_by=77, confidence=1.0))
    note = h.reports[-1].action_taken
    assert translate("report.boundary_departed_banned", "en") in note
    assert translate("report.boundary_already_banned", "en") not in note


async def test_already_banned_under_a_kick_policy_only_deletes() -> None:
    h = _Harness(target=_banned_already(), action=Action.DELETE_KICK)
    await h.coord.handle_verdict(_event(message_id=3))
    assert "kick_member" not in h.rest.calls
    assert "delete_message" in h.rest.calls
    assert translate("report.boundary_already_banned", "en") in h.reports[-1].action_taken


# --- Review of #81: the queue threshold and settlements without a ban -------


async def test_below_threshold_image_from_a_settled_uploader_is_left_alone() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    h.rest.calls.clear()
    # The harness's mod_queue_threshold is 0.5: this would not reach a moderator.
    result = await h.coord.handle_verdict(_risk_scan(message_id=11, confidence=0.4))
    assert "delete_message" not in h.rest.calls
    assert result.action is Action.NONE
    assert len(h.reports) == 1


async def test_settled_without_a_ban_keeps_the_risk_scan_card() -> None:
    # A Confirm under a delete policy settles the uploader, who is still here.
    h = _Harness(target=_departed(), action=Action.DELETE)
    await h.coord.handle_verdict(_event(message_id=3, confirmed_by=77, confidence=1.0))
    await h.coord.handle_verdict(_risk_scan(message_id=11))
    await h.coord.handle_verdict(_risk_scan(message_id=12, reported_by=5, confidence=1.0))
    assert len(h.reports) == 3  # both still ask a moderator


async def test_settled_without_a_ban_still_removes_hash_matches() -> None:
    h = _Harness(target=_departed(), action=Action.DELETE)
    await h.coord.handle_verdict(_event(message_id=3, confirmed_by=77, confidence=1.0))
    await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))
    assert len(h.reports) == 1
