"""Cross-channel campaign sweep for a confirmed scam.

A scam image is almost never posted once. The pattern this module exists for
is an account that pastes the same picture into every channel it can reach:
deleting the one message a moderator happened to click leaves the rest of the
campaign standing, and the copies keep converting for the scammer.

Discord's native ban purge (``delete_message_seconds``) already removes a
banned account's recent history across all channels, but it only fires *if the
ban succeeds*. When the ban is refused -- missing permission, role hierarchy,
the account already gone -- the purge silently never happens, which is exactly
how a "delete + ban" policy degrades into "deleted one message". This sweep is
the independent safety net: it works off the detection rows the bot has
already recorded for that uploader, so cleanup no longer depends on the ban.

It also harvests. A campaign usually varies the image slightly between posts
(recompression, crops, a changed border) to defeat exact matching. Every
distinct image the swept account posted is added to the guild blocklist, so
the *next* post of any of those variants is caught by the hash lane at upload
time rather than needing another moderator.

Deliberately scoped: only the uploader the moderator already confirmed as a
scammer, only within a bounded time window, only images the bot already has
rows for. It never touches other members' messages.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from prometheus_client import Counter

from optimus.core.logging import get_logger
from optimus.db.engine import SessionScope
from optimus.db.models import GuildHash
from optimus.db.repositories import DetectionRepository, GuildHashRepository

_log = get_logger(__name__)

SWEPT_MESSAGES = Counter(
    "optimus_sweep_messages_total",
    "Messages deleted by the cross-channel campaign sweep.",
    ["outcome"],
)
SWEPT_HASHES = Counter(
    "optimus_sweep_hashes_total",
    "Image hashes harvested from a swept campaign into the guild blocklist.",
)

#: Hash-ensemble keys stored on ``Detection.hashes`` (see ``_hash_ensemble_dict``).
_ENSEMBLE_KEYS = ("phash", "dhash", "whash", "ahash")

#: Deletes one message: ``(channel_id, message_id)``.
DeleteMessage = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SweepOutcome:
    """What one campaign sweep actually accomplished."""

    #: Messages successfully deleted (excludes the one already handled).
    deleted: int = 0
    #: Deletions that failed (already gone, no permission, channel deleted).
    failed: int = 0
    #: Channels the campaign had reached.
    channels: int = 0
    #: New blocklist hash ids harvested from the swept images.
    harvested: tuple[str, ...] = field(default=())

    @property
    def touched(self) -> bool:
        """Whether the sweep found anything beyond the original message."""
        return bool(self.deleted or self.failed or self.harvested)


class CampaignSweeper:
    """Purges a confirmed scammer's other posts and harvests their hashes."""

    def __init__(
        self,
        scope: SessionScope,
        *,
        delete_message: DeleteMessage,
        window_hours: int = 24,
        max_messages: int = 500,
    ) -> None:
        self._scope = scope
        self._delete = delete_message
        self._window_hours = window_hours
        self._max_messages = max_messages

    async def sweep(
        self,
        guild_id: int,
        *,
        uploader_id: int,
        skip_message_id: int,
        added_by: int,
    ) -> SweepOutcome:
        """Delete the uploader's other recent image posts and blocklist them.

        ``skip_message_id`` is the message the caller already actioned, so the
        sweep does not race the primary deletion and double-count it.

        Reads first, then writes: the message rows are collected in one short
        session, the REST deletions run with no transaction open (they are slow
        and must not hold SQLite's single write lock), and the harvested hashes
        are stored in a final short session. This is the same ordering the
        review command uses, for the same locking reason.
        """
        since = datetime.now(UTC) - timedelta(hours=self._window_hours)
        async with self._scope() as session:
            rows = await DetectionRepository(session, guild_id).list_by_uploader_since(
                uploader_id, since, limit=self._max_messages
            )
            # Detach the fields needed below so nothing touches a lazy
            # attribute after the session closes.
            targets = [
                (int(r.channel_id), int(r.message_id), dict(r.hashes or {}))
                for r in rows
                if int(r.message_id) != skip_message_id
            ]

        if not targets:
            return SweepOutcome()

        # One deletion per distinct message: the same message can have several
        # attachments and therefore several detection rows.
        seen: set[int] = set()
        deleted = failed = 0
        channels: set[int] = set()
        harvested: dict[str, dict[str, int]] = {}

        for channel_id, message_id, hashes in targets:
            ensemble = {k: int(hashes[k]) for k in _ENSEMBLE_KEYS if k in hashes}
            if len(ensemble) == len(_ENSEMBLE_KEYS):
                # Keyed by phash so repeated posts of one image collapse, while
                # a mutated variant registers as its own blocklist entry.
                harvested.setdefault(f"{ensemble['phash']:016x}", ensemble)
            if message_id in seen:
                continue
            seen.add(message_id)
            channels.add(channel_id)
            try:
                await self._delete(channel_id, message_id)
            except Exception as exc:
                # Routine and expected: the message may already be gone (the
                # ban purge may have beaten us to it), or the channel may be
                # unreadable. Never abort the rest of the campaign for one.
                failed += 1
                SWEPT_MESSAGES.labels(outcome="failed").inc()
                _log.debug(
                    "sweep_delete_failed",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    error=type(exc).__name__,
                )
                continue
            deleted += 1
            SWEPT_MESSAGES.labels(outcome="deleted").inc()

        stored = await self._harvest(guild_id, harvested, added_by=added_by)

        _log.info(
            "campaign_swept",
            guild_id=guild_id,
            uploader_id=uploader_id,
            deleted=deleted,
            failed=failed,
            channels=len(channels),
            harvested=len(stored),
        )
        return SweepOutcome(
            deleted=deleted,
            failed=failed,
            channels=len(channels),
            harvested=tuple(stored),
        )

    async def _harvest(
        self, guild_id: int, candidates: dict[str, dict[str, int]], *, added_by: int
    ) -> list[str]:
        """Add each distinct swept image to the guild blocklist.

        Existing entries are left alone rather than overwritten, so a hash a
        moderator added by hand keeps its original attribution.
        """
        if not candidates:
            return []
        stored: list[str] = []
        async with self._scope() as session:
            repo = GuildHashRepository(session, guild_id)
            for hash_id, ensemble in candidates.items():
                if await repo.get(hash_id) is not None:
                    continue
                await repo.add(
                    GuildHash(
                        hash_id=hash_id,
                        phash=ensemble["phash"],
                        dhash=ensemble["dhash"],
                        whash=ensemble["whash"],
                        ahash=ensemble["ahash"],
                        source="campaign_sweep",
                        added_by=added_by,
                    )
                )
                stored.append(hash_id)
                SWEPT_HASHES.inc()
        return stored
