"""The live Discord edges for simple mode: one gateway bot, both listeners.

Distributed mode runs the gateway and interactions as two separate processes,
each with its own :class:`hikari.GatewayBot`. Simple mode instead runs **one**
gateway connection and hangs both listeners off it:

* message/guild-join events drive the :class:`~optimus.services.gateway.bot.GatewayService`
  (publishing ``message_image.v1`` / ``guild_joined.v1`` onto the in-process bus);
* interaction events drive the :class:`~optimus.services.interactions.service.InteractionService`
  (slash commands and review buttons, answered ephemerally).

Both services read the same SQLite engine (via the app's session scope) and the
same in-memory store, so there is no second datastore to provision. This module
is pure Discord glue — the service logic it drives is unchanged from distributed
mode.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from optimus.core.config import Settings
from optimus.core.guild_config import GuildConfigCache
from optimus.core.logging import get_logger
from optimus.core.ratelimit import InMemoryRateLimiter
from optimus.core.readiness import shards_check
from optimus.db.repositories import DetectionRepository, GuildRepository
from optimus.i18n import translate
from optimus.services.gateway.access_watch import AccessWatcher
from optimus.services.gateway.bot import GATEWAY_INTENTS, GatewayService, shard_start_kwargs
from optimus.services.gateway.permission_probe import CachePermissionProbe, to_overwrites
from optimus.services.gateway.watchdog import GatewayWatchdog
from optimus.services.interactions.service import InteractionService, respond_to_interaction
from optimus.services.moderation.explain import (
    explain_rescan_summary,
    explain_setup_replay_summary,
)
from optimus.services.moderation.rest_adapter import HikariRestActions
from optimus.services.moderation.review import ReportData
from optimus.services.moderation.service import _post_report

if TYPE_CHECKING:
    from optimus.app.simple import SimpleApp

_log = get_logger(__name__)


async def run_discord_edges(  # pragma: no cover - requires a live gateway
    app: SimpleApp, settings: Settings, *, rest: object, bot_user_id: int
) -> None:
    """Connect one gateway bot wired to both the gateway and interactions edges.

    Blocks until the gateway disconnects (e.g. on interrupt). The caller owns the
    surrounding lifecycle (``app.aclose``, health/REST shutdown).
    """
    import hikari

    config_cache = GuildConfigCache(app.store, app._scope)
    bot = hikari.GatewayBot(token=settings.discord_token, intents=GATEWAY_INTENTS)

    # Let enforcement check its own permissions from the gateway cache before
    # calling Discord. In a channel the bot cannot see, this turns one failed
    # request per scam image into zero requests and a report card that says
    # which permission is missing where.
    probe = CachePermissionProbe(bot.cache, bot.rest, bot_user_id=bot_user_id)
    app.moderation.attach_permission_probe(probe)

    async def _fetch_message(channel_id: int, message_id: int) -> hikari.Message:
        return await bot.rest.fetch_message(channel_id, message_id)

    class _RestHistoryReader:
        """Join-backfill history access via the live bot's REST client."""

        async def list_text_channel_ids(self, guild_id: int) -> list[int]:
            channels = await bot.rest.fetch_guild_channels(guild_id)
            ids = [int(c.id) for c in channels if isinstance(c, hikari.TextableGuildChannel)]
            # fetch_guild_channels returns no threads, and forum channels are
            # not textable -- forum *posts* are threads. Without this, a scam
            # wave living in threads/forum posts is invisible to the join
            # backfill. Regular channels stay first so the max-channels cap
            # prefers them; threads are best-effort (older API surface).
            try:
                threads = await bot.rest.fetch_active_threads(guild_id)
            except hikari.HikariError:
                _log.warning("join_backfill_threads_failed", guild_id=guild_id, exc_info=True)
                return ids
            seen = set(ids)
            for thread in threads:
                if isinstance(thread, hikari.TextableGuildChannel) and int(thread.id) not in seen:
                    ids.append(int(thread.id))
            return ids

        async def fetch_recent_messages(
            self, channel_id: int, *, after: datetime, limit: int
        ) -> list[hikari.Message]:
            # Newest-first, stopping at the look-back cutoff, capped at `limit`:
            # when a busy channel has more than `limit` messages in the window,
            # the *newest* ones (most likely to still be live scams) are kept.
            iterator = (
                bot.rest.fetch_messages(channel_id)
                .take_while(lambda m: m.created_at >= after)
                .limit(limit)
            )
            return list(await iterator)

    async def _has_review_channel(guild_id: int) -> bool:
        """Gate for the join backfill: does this guild already have a review channel?

        Called by :class:`GatewayService` before running the always-on join
        scan. When the guild has no review channel linked, the scan is
        deferred until ``/setup`` fires the sibling ``on_review_channel_linked``
        hook below -- otherwise every card the scan produced would land in
        ``_post_report`` with ``review_channel_id is None`` and be dropped
        silently. Cheap: a single indexed lookup on the ``guilds`` PK.
        """
        async with app._scope() as session:
            guild = await GuildRepository(session).get(guild_id)
        return guild is not None and guild.review_channel_id is not None

    gateway = GatewayService(
        settings,
        app.bus,
        config_cache,
        app.health,
        fetch_message=_fetch_message,
        history=_RestHistoryReader(),
        has_review_channel=_has_review_channel,
    )

    async def _rescan(guild_id: int, channel_ids: Sequence[int]) -> None:
        """Rescan channels the bot has just regained access to, then say so.

        The rescan republishes through the ordinary live-scan path, so anything
        scam-like posted while the bot was blocked lands as a normal review
        card. The summary line exists so a moderator who has just fixed an
        overwrite can see the fix took effect.
        """
        probe.forget(guild_id)
        channels, messages = await gateway.rescan_channels(
            guild_id, channel_ids, trigger="access_regained"
        )
        _log.info(
            "access_regained_rescan",
            guild_id=guild_id,
            channels=channels,
            messages=messages,
        )
        if not channels:
            return
        async with app._scope() as session:
            guild = await GuildRepository(session).get(guild_id)
        if guild is None or guild.review_channel_id is None:
            return
        summary = explain_rescan_summary(tuple(channel_ids), messages, guild.locale)
        with contextlib.suppress(Exception):
            await bot.rest.create_message(guild.review_channel_id, summary)

    access = AccessWatcher(probe, _rescan, bot_user_id=bot_user_id)

    # How far back the /setup backlog replay looks (days). Aligned with
    # InteractionService.has_pending_scan so /config view and the replay
    # agree on what counts as "pending".
    setup_replay_days = 3
    # Hard cap on how many detections the /setup replay posts. A guild joined
    # mid-scam-wave can accumulate thousands of unreported detections; dumping
    # all of them into a fresh review channel makes the queue unreadable and
    # burns rate limit. Newest-first, so the ones most likely still live make
    # the cut.
    setup_replay_limit = 50

    async def _on_review_channel_linked(guild_id: int) -> None:
        """Post the pre-setup detection backlog and run the deferred join scan.

        Called from :class:`InteractionService`'s post-commit hook, i.e.
        *after* the ``review_channel_id`` write has landed, so a fresh read
        of the guild here sees the new channel. Runs three things, isolated
        so one failing does not silently swallow the others:

        1. Replay: newest unreported detections in the last 3 days (cap 50)
           are posted as review cards and stamped ``reported_at``. Older or
           over-cap detections stay unreported forever -- see the summary
           line's "N more not shown" caveat.
        2. Summary line so the burst of cards reads as a catch-up, not as
           an active incident wave.
        3. Deferred join backfill for the guild's recent history, if the
           original ``on_guild_join`` deferred it here.
        """
        async with app._scope() as session:
            guild = await GuildRepository(session).get(guild_id)
            if guild is None or guild.review_channel_id is None:
                return
            channel_id = guild.review_channel_id
            locale = guild.locale
            det_repo = DetectionRepository(session, guild_id)
            since = datetime.now(UTC) - timedelta(days=setup_replay_days)
            total_pending = await det_repo.count_unreported_since(since)
            pending = await det_repo.list_unreported_since(since, limit=setup_replay_limit)
        posted = 0
        for detection in pending:
            data = ReportData(
                detection_id=detection.id,
                guild_id=guild_id,
                channel_id=detection.channel_id,
                message_id=detection.message_id,
                uploader_id=detection.uploader_id,
                verdict=detection.verdict,
                # Confidence is a runtime property of the verdict -- not
                # persisted on the row -- so replayed cards omit the line.
                confidence=None,
                action_taken=detection.action_taken,
                locale=locale,
            )
            # Reuse the moderation service's own post helper so replayed cards
            # go through exactly the same render + button wiring as live ones
            # (no drift between the two code paths on future review-card
            # tweaks).
            try:
                posted_message_id = await _post_report(bot.rest, channel_id, data)
            except Exception:
                _log.warning(
                    "setup_replay_post_failed",
                    guild_id=guild_id,
                    detection_id=detection.id,
                    exc_info=True,
                )
                continue
            if posted_message_id is None:
                # _post_report returned None -- the rest call itself did not
                # raise but did not produce a message either. Skip the stamp
                # so the row stays eligible for the next /setup re-run.
                continue
            # Stamp per-row rather than in one UPDATE at the end: a mid-loop
            # crash then leaves the un-posted tail eligible to be picked up
            # again on the next ``/setup`` re-run, not double-posted.
            async with app._scope() as session:
                await DetectionRepository(session, guild_id).set_reported_at(
                    detection.id, datetime.now(UTC)
                )
            _log.info(
                "setup_replay_posted",
                guild_id=guild_id,
                detection_id=detection.id,
                message_id=int(posted_message_id),
            )
            posted += 1
        if posted > 0:
            more = max(total_pending - posted, 0)
            summary = explain_setup_replay_summary(posted, more, setup_replay_days, locale)
            with contextlib.suppress(Exception):
                await bot.rest.create_message(channel_id, summary)
        _log.info(
            "setup_replay_complete",
            guild_id=guild_id,
            posted=posted,
            pending=total_pending,
            capped=total_pending > setup_replay_limit,
        )
        # Kick off the deferred join backfill in the background: the
        # interaction task is already off the critical path (post-commit
        # hook) but this scan can still take seconds on a big guild, and
        # blocking here would delay the next handler off the event loop.
        gateway.track(asyncio.create_task(gateway.run_deferred_join_backfill(guild_id)))

    interactions = InteractionService(
        app._scope,
        InMemoryRateLimiter(),
        settings,
        detection=app.detection,
        # Review buttons enforce through REST: Confirm scam deletes the
        # message, Ban/Unban act on the uploader, and member-report cards
        # (filed without hashes by design) re-fetch the attachment to hash it.
        rest=HikariRestActions(bot.rest),
        # Same probe as enforcement, so the reply a moderator sees and the
        # action the bot later attempts agree about what is possible.
        probe=probe,
        # /config permissions audits every channel from the same cache.
        inventory=probe,
        # See :meth:`InteractionService._run` -- these two power Fix 1
        # (scan-policy cache invalidated on ``/config set``) and Fix 3
        # (backlog replay + deferred join backfill on ``/setup``).
        config_cache=config_cache,
        on_review_channel_linked=_on_review_channel_linked,
    )

    # Readiness should track the gateway, not just the DB: a wedged gateway
    # session (the "commands all time out" incident) previously left /readyz
    # green because only the database check was registered in simple mode.
    app.health.add_readiness_check(shards_check(bot), name="shards")
    watchdog = GatewayWatchdog(
        bot,
        app.health,
        interval_seconds=settings.gateway_watchdog_interval_seconds,
        stale_exit_seconds=settings.gateway_stale_exit_seconds,
    )

    @bot.listen(hikari.GuildMessageCreateEvent)
    async def _on_message(event: hikari.GuildMessageCreateEvent) -> None:
        await gateway.on_message(event)

    @bot.listen(hikari.GuildMessageUpdateEvent)
    async def _on_message_update(event: hikari.GuildMessageUpdateEvent) -> None:
        await gateway.on_message_update(event)

    @bot.listen(hikari.GuildJoinEvent)
    async def _on_guild_join(event: hikari.GuildJoinEvent) -> None:
        # DM the owner a one-line prompt to run /setup. Without a review
        # channel the moderation pipeline persists detections but drops the
        # cards silently -- the owner has no way to see the bot is "waiting
        # on you" unless we tell them. Best-effort: DMs closed / owner not
        # DMable is not an error -- the same prompt shows up as the
        # pending-scan line in /config view later. Fired in the background so
        # the DM latency does not delay the join backfill.
        guild = event.guild
        if guild is not None and guild.owner_id is not None:

            async def _dm_owner(owner_id: int, guild_name: str) -> None:
                try:
                    channel = await bot.rest.create_dm_channel(owner_id)
                    await bot.rest.create_message(
                        channel.id,
                        translate("command.join_dm", "en", guild_name=guild_name),
                    )
                except Exception:
                    _log.info(
                        "guild_join_owner_dm_failed",
                        guild_id=int(event.guild_id),
                        owner_id=owner_id,
                        exc_info=True,
                    )

            gateway.track(asyncio.create_task(_dm_owner(int(guild.owner_id), guild.name)))
        await gateway.on_guild_join(event)

    @bot.listen(hikari.GuildChannelUpdateEvent)
    async def _on_channel_update(event: hikari.GuildChannelUpdateEvent) -> None:
        # An overwrite edit is how a moderator un-blinds the bot. Without this
        # the fix took effect only for *future* uploads: scam images already
        # sitting in the channel stayed up, which is exactly what happened in
        # the reported incident.
        await access.channel_updated(
            int(event.guild_id),
            int(event.channel.id),
            before=(to_overwrites(event.old_channel) if event.old_channel is not None else None),
            after=to_overwrites(event.channel),
        )

    @bot.listen(hikari.RoleUpdateEvent)
    async def _on_role_update(event: hikari.RoleUpdateEvent) -> None:
        # The other way access is granted: editing a role the bot already holds.
        # One role edit can un-blind many channels at once, so each is evaluated
        # with the old bits and the new ones and only the flips are rescanned.
        channels = probe.guild_channels(int(event.guild_id))
        if channels is None:
            return
        await access.role_updated(
            int(event.guild_id),
            int(event.role_id),
            before_permissions=(
                int(event.old_role.permissions) if event.old_role is not None else None
            ),
            after_permissions=int(event.role.permissions),
            channels=channels,
        )

    @bot.listen(hikari.InteractionCreateEvent)
    async def _on_interaction(event: hikari.InteractionCreateEvent) -> None:
        interaction = event.interaction
        if not isinstance(interaction, hikari.CommandInteraction | hikari.ComponentInteraction):
            return
        await respond_to_interaction(interactions, interaction)

    try:
        await bot.start(**shard_start_kwargs(settings))  # type: ignore[arg-type]
        watchdog.start()
        await bot.join()
    finally:
        await watchdog.stop()
        await access.aclose()
        await gateway.drain()
        with contextlib.suppress(Exception):
            await bot.close()
