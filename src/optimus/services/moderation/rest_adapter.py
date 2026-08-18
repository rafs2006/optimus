"""Adapter mapping the executor's ``RestActions`` protocol onto hikari's client.

The moderation executor speaks the small :class:`~optimus.services.moderation.actions.RestActions`
protocol (``ban_member(guild_id, user_id, reason)`` and friends). hikari's real
REST client has different names and keyword-only parameters — ``ban_user(guild,
user, *, reason=...)``, no ``timeout_member``, no ``send_dm`` — so handing the
raw client to the executor made every punitive action raise ``TypeError`` or
``AttributeError`` at runtime, while protocol-shaped test doubles kept the suite
green. This adapter is the one place where the protocol meets hikari's true
signatures, and its tests bind against ``create_autospec(RESTClientImpl)`` so a
signature drift fails the suite instead of production.

Attributes outside the protocol (``fetch_member``, ``fetch_guild``,
``fetch_roles``, ``create_message``, ...) delegate to the wrapped client, so the
coordinator's target resolver and report poster keep working against the same
object.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import hikari


class HikariRestActions:
    """:class:`RestActions` implemented against a real hikari REST client."""

    def __init__(self, rest: Any) -> None:
        self._rest = rest

    def __getattr__(self, name: str) -> Any:
        # Non-protocol surface passes straight through to hikari.
        return getattr(self._rest, name)

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        """Delete a message; an already-deleted message counts as success.

        The goal is "message gone", so a 404 is not a failure — this also makes
        a manual re-run of ``reviewmsg`` on an already-removed message idempotent
        instead of erroring out before the punitive step.
        """
        with contextlib.suppress(hikari.NotFoundError):
            await self._rest.delete_message(channel_id, message_id)

    async def timeout_member(self, guild_id: int, user_id: int, seconds: int) -> None:
        until = datetime.now(UTC) + timedelta(seconds=seconds)
        await self._rest.edit_member(guild_id, user_id, communication_disabled_until=until)

    async def kick_member(self, guild_id: int, user_id: int, reason: str) -> None:
        await self._rest.kick_user(guild_id, user_id, reason=reason)

    async def ban_member(
        self, guild_id: int, user_id: int, reason: str, purge_seconds: int = 0
    ) -> None:
        # delete_message_seconds is Discord's native cross-channel purge — the
        # same "delete message history" option as the manual ban dialog (max 7d).
        await self._rest.ban_user(
            guild_id, user_id, delete_message_seconds=purge_seconds, reason=reason
        )

    async def unban_member(self, guild_id: int, user_id: int, reason: str) -> None:
        await self._rest.unban_user(guild_id, user_id, reason=reason)

    async def fetch_attachment_url(
        self, channel_id: int, message_id: int, attachment_id: int
    ) -> str | None:
        """Return a fresh CDN URL for one attachment, or ``None`` if it is gone.

        Discord CDN URLs are signed and expire, so anything stored at detection
        time may be stale; re-fetching the message mints a fresh URL. A deleted
        message (or a detached attachment) yields ``None``, never an exception
        -- callers treat "image gone" as a soft, reportable condition.
        """
        try:
            message = await self._rest.fetch_message(channel_id, message_id)
        except hikari.NotFoundError:
            return None
        for attachment in message.attachments:
            if int(attachment.id) == attachment_id:
                return str(attachment.url)
        return None

    async def create_review_channel(
        self, guild_id: int, *, name: str, mod_role_ids: list[int]
    ) -> int:
        """Create the private mod-review text channel and return its id.

        Visibility is locked down at creation time (never patched afterwards,
        so there is no window where the channel is public): ``@everyone`` is
        denied VIEW_CHANNEL, the bot itself and each ``mod_role_ids`` role are
        allowed to view/read/send. Guild administrators bypass overwrites by
        Discord's permission model, so they always see the channel even when
        no mod role is passed.
        """
        me = await self._rest.fetch_my_user()
        member_allow = (
            hikari.Permissions.VIEW_CHANNEL
            | hikari.Permissions.SEND_MESSAGES
            | hikari.Permissions.READ_MESSAGE_HISTORY
            | hikari.Permissions.EMBED_LINKS
            | hikari.Permissions.ATTACH_FILES
        )
        overwrites: list[hikari.PermissionOverwrite] = [
            # The @everyone role's id is always the guild id.
            hikari.PermissionOverwrite(
                id=hikari.Snowflake(guild_id),
                type=hikari.PermissionOverwriteType.ROLE,
                deny=hikari.Permissions.VIEW_CHANNEL,
            ),
            hikari.PermissionOverwrite(
                id=me.id,
                type=hikari.PermissionOverwriteType.MEMBER,
                allow=member_allow,
            ),
        ]
        overwrites.extend(
            hikari.PermissionOverwrite(
                id=hikari.Snowflake(role_id),
                type=hikari.PermissionOverwriteType.ROLE,
                allow=member_allow,
            )
            for role_id in mod_role_ids
        )
        channel = await self._rest.create_guild_text_channel(
            guild_id,
            name,
            permission_overwrites=overwrites,
            topic="Optimus scam-image review queue -- detections land here for moderator sign-off.",
        )
        return int(channel.id)

    async def fetch_owner_ids(self) -> set[int]:
        """User ids that own this application (single owner or team members).

        Gates the owner-only ``/global`` command. Cached after the first
        fetch — ownership changes require re-inviting the bot anyway, and the
        gate must not cost a REST round-trip per command.
        """
        cached: set[int] | None = self.__dict__.get("_owner_ids")
        if cached is not None:
            return cached
        app = await self._rest.fetch_application()
        ids: set[int] = set()
        if app.owner is not None:
            ids.add(int(app.owner.id))
        if app.team is not None:
            ids.update(int(member_id) for member_id in app.team.members)
        self.__dict__["_owner_ids"] = ids
        return ids

    async def send_dm(self, user_id: int, content: str) -> None:
        channel = await self._rest.create_dm_channel(user_id)
        await self._rest.create_message(channel.id, content)
