"""A post handled automatically folds its own open card, whatever the image order.

Reported case (#1520): one post of 4 images. Three were near matches (0.71 to
0.82) and went first: they opened a card with buttons. The fourth was a strong
match (0.99): the bot deleted the post, banned the uploader and swept the
campaign. The cleanup closes the uploader's *other* posts' cards only, so the
same post's open card stayed open, with buttons, for a post that was gone. A
moderator had to press Confirm.

Now the strong image adopts that card: the earlier images are recorded as
handled with the post, and the card is folded and re-rendered in place.
"""

from __future__ import annotations

import asyncio
from typing import Any

from optimus.services.moderation.review import (
    AUTO_HANDLED_BUTTONS,
    build_action_rows,
    build_card,
    merge_reports,
)
from tests.unit.test_departed_ban_auto_close import _departed, _event, _Harness


class _SettleHarness(_Harness):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.settled: list[tuple[int, tuple[int, ...], str]] = []
        self.coord._settle_detections = self._settle

    async def _settle(self, guild_id: int, detection_ids: Any, action: str) -> None:
        self.settled.append((guild_id, tuple(detection_ids), action))


def _post() -> list:
    # The order of the #1520 logs: three near matches, then the strong one.
    return [
        _event(attachment_id=1, confidence=0.77),
        _event(attachment_id=2, confidence=0.82),
        _event(attachment_id=3, confidence=0.71),
        _event(attachment_id=4, confidence=0.99),
    ]


async def _run(h: _Harness) -> None:
    for e in _post():
        await h.coord.handle_verdict(e)


async def test_the_open_card_folds_when_a_later_image_auto_acts() -> None:
    h = _SettleHarness(target=_departed())
    await _run(h)

    assert len(h.reports) == 1  # no second card for the strong image
    card_id, items = h.updates[-1]
    assert card_id == 7  # the card the near matches opened
    assert len(items) == 4
    merged = merge_reports(items)
    assert merged.auto_handled
    assert merged.decided_by is None
    # Folded: only the False positive button is left.
    embeds, rows = build_card(items)
    assert len(embeds) == 1
    assert repr(rows) == repr(build_action_rows(merged.detection_id, AUTO_HANDLED_BUTTONS))


async def test_the_earlier_images_are_stored_as_handled() -> None:
    h = _SettleHarness(target=_departed())
    await _run(h)

    # The near matches' rows (101-103) no longer read as open questions.
    assert h.settled == [(1, (101, 102, 103), "auto:delete")]
    assert [a for a, _ok in h.audits][-1] == "auto:delete_ban"
    _card_id, items = h.updates[-1]
    assert [i.action_taken for i in items[:3]] == ["delete"] * 3
    assert all(i.image_url is None for i in items)  # the post is gone


async def test_a_refused_ban_keeps_the_card_open() -> None:
    h = _SettleHarness(target=_departed())

    class _LimitError(Exception):
        code = 30035
        status = 400

    async def _refuse(*_a: Any, **_k: Any) -> None:
        raise _LimitError

    h.rest.ban_member = _refuse  # type: ignore[method-assign]
    await _run(h)

    assert h.settled == []
    _card_id, items = h.updates[-1]
    assert not merge_reports(items).auto_handled


async def test_strong_image_first_still_makes_one_folded_card() -> None:
    h = _SettleHarness(target=_departed())
    await asyncio.gather(*(h.coord.handle_verdict(e) for e in reversed(_post())))

    assert len(h.reports) == 1
    _card_id, items = h.updates[-1]
    assert merge_reports(items).auto_handled
    assert h.settled == []  # nothing was ever open
