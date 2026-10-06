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

import pytest
from structlog.testing import capture_logs

from optimus.contracts.events import Action
from optimus.services.moderation import coordinator as coordinator_module
from optimus.services.moderation.review import merge_reports
from tests.unit.test_departed_ban_auto_close import _data, _departed, _event, _Harness

# --- Within the settled window ----------------------------------------------


async def test_global_match_from_a_settled_uploader_is_removed_quietly() -> None:
    h = _Harness(target=_departed())
    await h.coord.handle_verdict(_event(message_id=3))
    result = await h.coord.handle_verdict(_event(message_id=11, matched_source="global"))

    assert result.action is Action.DELETE
    assert len(h.reports) == 1  # no open card for the global match
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


async def test_member_report_from_a_settled_uploader_keeps_its_card() -> None:
    h = _Harness(target=_departed())
    h.coord._mark_settled(_event(message_id=3))
    await h.coord.handle_verdict(_event(message_id=11, matched_source="global", reported_by=5))
    assert h.closes == []


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
