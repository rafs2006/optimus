"""Database reads for "did a moderator take this back?".

False positive, Whitelist image and Dismiss are pressed in the interactions
service, which may be another process, so the moderation coordinator asks the
database rather than its own memory. Two rules use this: the spread rule
drops channels a moderator cleared, and a settlement ends once a moderator
cleared any of the uploader's cards after it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta

from optimus.db.engine import SessionScope
from optimus.db.repositories import DetectionRepository, ModActionRepository

#: A moderator's "this is not a scam" on a card: False positive and Dismiss
#: mark the row, Whitelist image only writes the audit log.
CLEARED_ACTIONS = frozenset({"reversed", "dismissed"})
CLEARED_AUDIT_ACTIONS = ("review.false_positive", "review.whitelist_image", "review.dismiss")

#: How far before a settlement its cards' detections may have been recorded:
#: a moderator can press Confirm minutes after the post.
SETTLED_CARD_LOOKBACK = timedelta(hours=1)


def cleared_channels_query(
    scope: SessionScope, limit: int
) -> Callable[[int, int, datetime, Sequence[int]], Awaitable[set[int]]]:
    """(guild, uploader, since, channels) -> channels where a moderator cleared a card."""

    async def cleared(
        guild_id: int, uploader_id: int, since: datetime, channels: Sequence[int]
    ) -> set[int]:
        wanted = set(channels)
        async with scope() as session:
            rows = [
                r
                for r in await DetectionRepository(session, guild_id).list_by_uploader_since(
                    uploader_id, since, limit=limit
                )
                if int(r.channel_id) in wanted
            ]
            audited = await ModActionRepository(session, guild_id).targets_since(
                CLEARED_AUDIT_ACTIONS, [str(r.id) for r in rows], since
            )
        return {
            int(r.channel_id)
            for r in rows
            if r.action_taken in CLEARED_ACTIONS or str(r.id) in audited
        }

    return cleared


def settlement_cleared_query(
    scope: SessionScope, limit: int
) -> Callable[[int, int, datetime], Awaitable[bool]]:
    """(guild, uploader, since) -> whether a moderator cleared any of their cards since then.

    Only button presses after ``since`` count, read from the audit log: a
    row's ``action_taken`` has no time of its own, and an earlier, unrelated
    False positive must not end a later settlement.
    """

    async def cleared(guild_id: int, uploader_id: int, since: datetime) -> bool:
        async with scope() as session:
            rows = await DetectionRepository(session, guild_id).list_by_uploader_since(
                uploader_id, since - SETTLED_CARD_LOOKBACK, limit=limit
            )
            audited = await ModActionRepository(session, guild_id).targets_since(
                CLEARED_AUDIT_ACTIONS, [str(r.id) for r in rows], since
            )
        return bool(audited)

    return cleared
