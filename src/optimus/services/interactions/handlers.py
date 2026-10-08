"""Server-side interaction handlers: auth, side effects, audit — hikari-free.

Each slash command and component (button) press is reduced to a plain
:class:`InteractionContext` (who, where, which command, which options) and
dispatched here. Handlers run the *server-side* permission re-check, perform the
database/Redis side effects through injected dependencies, write a
``mod_actions`` audit row for every state change, and return an
:class:`InteractionResponse` (always ephemeral) carrying an i18n key.

Nothing in this module imports hikari, so the permission matrix, audit
behaviour, and appeal lifecycle are fully unit-testable. The hikari/REST/DB
wiring that produces an :class:`InteractionContext` and renders an
:class:`InteractionResponse` lives in :mod:`.service`.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from optimus.core.logging import get_logger
from optimus.db.models import GuildHash, GuildWhitelist
from optimus.i18n import translate
from optimus.services.detection.matcher import DEFAULT_WHITELIST_RADIUS
from optimus.services.interactions.attachment_hash import (
    AttachmentHashError,
    AttachmentHashes,
)
from optimus.services.interactions.commands import ALSO_ACCEPTED, required_permission
from optimus.services.interactions.logic import (
    AddProblem,
    CommandError,
    ComponentAction,
    InteractionRejected,
    Permission,
    build_export,
    has_permission,
    validate_config_set,
    validate_import,
)
from optimus.services.interactions.logic import (
    ImportHash as _ImportHash,
)
from optimus.services.moderation import reasons
from optimus.services.moderation.explain import explain_access_report
from optimus.services.moderation.permissions import AccessReport
from optimus.services.moderation.review import (
    BUTTON_LABELS,
    ParsedCustomId,
    ReportData,
    ReviewAction,
    jump_url,
)
from optimus.services.moderation.service import SYSTEM_ACTOR

_log = get_logger(__name__)

#: Keep ``/scamhash list`` safely below Discord's 2,000-character message limit
#: even if every rendered line maxes out (64-char hash id, 32-char source, and
#: a full-width ``by <@user>`` mention ≈ 130 chars per line).
#: ``/scamhash list`` shows the newest entries only; export has the rest.
_HASH_LIST_PREVIEW_LIMIT = 10


@dataclass(frozen=True, slots=True)
class InteractionContext:
    """Everything a handler needs about one invocation, gateway-agnostic."""

    guild_id: int | None
    user_id: int
    #: The invoking member's *effective* permission bitfield (never the hint).
    member_permissions: int
    command: str
    subcommand: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    locale: str = "en"
    #: For a button press: the message id of the card the button is on, so a
    #: review action can cover every image shown on that card.
    card_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class InteractionResponse:
    """An always-ephemeral reply, identified by an i18n key plus params."""

    i18n_key: str
    params: dict[str, Any] = field(default_factory=dict)
    #: Optional opaque payload (e.g. an export file body) for the glue layer.
    attachment: str | None = None
    #: When set, the glue layer appends this localized line to the message the
    #: pressed button lives on (the review card), so every moderator watching
    #: the shared review channel sees who already handled the report -- the
    #: ephemeral reply above is visible only to the clicker.
    card_note_key: str | None = None
    card_note_params: dict[str, Any] = field(default_factory=dict)


class SetupFailure(StrEnum):
    """Why ``/setup`` could not create the review channel.

    Discord refuses channel creation for reasons that need opposite fixes --
    granting a permission, deleting a channel, dropping the ``mod_role``,
    or simply waiting -- and the single "grant Manage Channels" reply sent
    moderators to fix a permission that was usually not the problem.

    Kept free of hikari types so the handler layer stays transport-agnostic;
    :class:`~optimus.services.interactions.service.DbDeps` owns the mapping
    from Discord's exceptions and JSON error codes onto these.
    """

    NO_PERMISSION = "no_permission"
    CHANNEL_LIMIT = "channel_limit"
    OVERWRITE_LIMIT = "overwrite_limit"
    RATE_LIMITED = "rate_limited"
    DISCORD_DOWN = "discord_down"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ChannelCreation:
    """The outcome of one review-channel creation attempt.

    Exactly one of ``channel_id`` / ``failure`` is set. ``retry_seconds`` is
    populated only for :attr:`SetupFailure.RATE_LIMITED`, where telling the
    moderator *how long* is the whole difference between actionable advice
    and "try again" -- which they just did.
    """

    channel_id: int | None = None
    failure: SetupFailure | None = None
    retry_seconds: int | None = None

    @classmethod
    def created(cls, channel_id: int) -> ChannelCreation:
        return cls(channel_id=channel_id)

    @classmethod
    def failed(cls, failure: SetupFailure, *, retry_seconds: int | None = None) -> ChannelCreation:
        return cls(failure=failure, retry_seconds=retry_seconds)


#: Which reply each failure earns. Every one of these strings ends by pointing
#: at ``/setup channel:``, the escape hatch that needs no Manage Channels at
#: all -- so a moderator is never left with only a fix they cannot apply.
SETUP_FAILURE_KEYS: dict[SetupFailure, str] = {
    SetupFailure.NO_PERMISSION: "command.setup_failed_permission",
    SetupFailure.CHANNEL_LIMIT: "command.setup_failed_channel_limit",
    SetupFailure.OVERWRITE_LIMIT: "command.setup_failed_overwrite_limit",
    SetupFailure.RATE_LIMITED: "command.setup_failed_rate_limited",
    SetupFailure.DISCORD_DOWN: "command.setup_failed_discord_down",
    SetupFailure.UNAVAILABLE: "command.setup_failed_unavailable",
    SetupFailure.UNKNOWN: "command.setup_failed",
}


#: The Discord permission each review-card button requires of the clicker.
#:
#: Keyed to what the button *does*, so the moderators a server already trusts
#: with that power can use it. The old blanket ``MANAGE_GUILD`` got this wrong
#: in both directions: most servers' mods hold Ban/Manage Messages but not
#: Manage Server, so they saw every card and could press nothing; while a
#: Manage Server holder who was deliberately denied Ban Members could ban
#: through the bot anyway.
#:
#: ``FALSE_POSITIVE`` can lift a ban, but only for a clicker who also holds
#: Ban Members -- the handler checks that itself -- so it stays on Manage
#: Messages and every moderator can still correct a wrong call.
#:
#: ``CONFIRM_SCAM`` deletes the message and then applies the server's
#: ``action_policy``, which may ban. That ban is still Manage Messages, on
#: purpose: the policy is the admins' standing decision about what a confirmed
#: match gets, the same one the automatic pipeline enforces with no human in
#: the loop at all. Confirm asserts "this is a match"; ``BAN_UPLOADER`` is the
#: discretionary ban, and that is the one gated on Ban Members.
REVIEW_ACTION_PERMISSIONS: dict[ReviewAction, Permission] = {
    ReviewAction.CONFIRM_SCAM: Permission.MANAGE_MESSAGES,
    ReviewAction.FALSE_POSITIVE: Permission.MANAGE_MESSAGES,
    ReviewAction.DISMISS: Permission.MANAGE_MESSAGES,
    ReviewAction.WHITELIST_IMAGE: Permission.MANAGE_MESSAGES,
    ReviewAction.BAN_UPLOADER: Permission.BAN_MEMBERS,
    ReviewAction.UNBAN: Permission.BAN_MEMBERS,
    # Retired: the handler only answers that the button is gone. Still gated so
    # a stale card left in an old review channel stays mod-only like the rest.
    ReviewAction.SUBMIT_GLOBAL: Permission.MANAGE_MESSAGES,
}


def review_action_permission(action: ReviewAction) -> Permission:
    """The permission ``action`` requires; unmapped actions fail closed.

    An action missing from :data:`REVIEW_ACTION_PERMISSIONS` falls back to
    ``MANAGE_GUILD`` -- the strictest non-admin bar -- rather than to no check.
    """
    return REVIEW_ACTION_PERMISSIONS.get(action, Permission.MANAGE_GUILD)


@dataclass(frozen=True, slots=True)
class DetectionFacts:
    """The stored facts a review button needs about one detection."""

    detection_id: int
    channel_id: int
    message_id: int
    attachment_id: int
    uploader_id: int
    #: ``HashSet.model_dump()`` captured at detection time; ``None`` for member
    #: reports (never hashed by design) and rows predating migration 0008.
    hashes: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class KnownImage:
    """What this server's lists already say about an image ``/scamhash add`` got.

    ``entry`` is the blocklist entry that already covers the image: the same
    image (``exact``), or one the scanner would already flag with confidence
    (a re-saved, resized or re-compressed copy).
    """

    entry: GuildHash | None = None
    exact: bool = False


@dataclass(frozen=True, slots=True)
class LinkResult:
    """Servers in a link group after ``/global link_server``, and what each gained."""

    members: tuple[int, ...]
    gained: dict[int, int]


@dataclass(frozen=True, slots=True)
class ImageHashes:
    """A resolved hash ensemble for the image behind a detection.

    Mirror hashes are present only when the image was re-fetched and re-hashed
    (stored detection hashes keep the 4-hash ensemble of ``contracts.HashSet``).
    """

    phash: int
    dhash: int
    whash: int
    ahash: int
    mphash: int | None = None
    mdhash: int | None = None
    mwhash: int | None = None
    mahash: int | None = None


class ModerationRest(Protocol):
    """The Discord REST surface the review buttons enforce through.

    Structurally satisfied by
    :class:`optimus.services.moderation.rest_adapter.HikariRestActions`;
    declared here so this module stays hikari-free and tests can fake it.
    """

    async def delete_message(self, channel_id: int, message_id: int) -> None: ...

    async def ban_member(
        self, guild_id: int, user_id: int, reason: str, purge_seconds: int = 0
    ) -> None: ...

    async def unban_member(self, guild_id: int, user_id: int, reason: str) -> None: ...

    async def fetch_attachment_url(
        self, channel_id: int, message_id: int, attachment_id: int
    ) -> str | None: ...

    async def create_review_channel(
        self, guild_id: int, *, name: str, mod_role_ids: list[int]
    ) -> int: ...

    async def fetch_owner_ids(self) -> set[int]: ...

    async def post_review_card(self, channel_id: int, items: Sequence[ReportData]) -> int:
        """Post one (grouped) review card with its buttons; return its message id."""
        ...


class InteractionDeps(Protocol):
    """Side-effecting collaborators a handler needs, all per-request scoped."""

    async def add_guild_hash(self, guild_id: int, gh: GuildHash) -> GuildHash: ...
    async def known_image(self, guild_id: int, hashes: AttachmentHashes) -> KnownImage: ...
    async def remove_guild_hash(self, guild_id: int, hash_id: str) -> int: ...
    async def list_guild_hashes(self, guild_id: int) -> list[GuildHash]: ...
    async def add_whitelist(self, guild_id: int, entry: GuildWhitelist) -> GuildWhitelist: ...
    async def list_whitelist(self, guild_id: int) -> list[GuildWhitelist]: ...
    async def remove_whitelist(self, guild_id: int, entry_ids: Sequence[int]) -> int: ...
    async def get_config(self, guild_id: int) -> dict[str, Any]: ...

    def auto_act_threshold(self) -> float:
        """The deployment-wide confidence at which automatic action starts.

        ``mod_queue_threshold`` may not exceed it: the policy engine requires
        review to start at or below the point where action does.
        """
        ...

    async def set_config_field(self, guild_id: int, field: str, value: Any) -> None: ...
    async def stats_summary(self, guild_id: int) -> dict[str, Any]: ...
    async def open_queue(self, guild_id: int, *, limit: int) -> dict[str, Any]: ...
    async def purge_guild(self, guild_id: int) -> int: ...
    async def detection_belongs_to(
        self, guild_id: int, detection_id: int, user_id: int
    ) -> bool: ...
    async def open_appeal(self, guild_id: int, detection_id: int, user_id: int) -> int: ...
    async def get_appeal(self, guild_id: int, appeal_id: int) -> dict[str, Any] | None: ...
    async def resolve_appeal(self, guild_id: int, appeal_id: int, *, approved: bool) -> None: ...
    async def reverse_detection_action(self, guild_id: int, detection_id: int) -> None: ...
    async def get_detection(self, guild_id: int, detection_id: int) -> DetectionFacts | None: ...
    async def get_card_detections(
        self, guild_id: int, card_message_id: int
    ) -> list[DetectionFacts]:
        """Every detection on one review card (empty for pre-0012 cards)."""
        ...

    async def get_message_detections(self, guild_id: int, message_id: int) -> list[DetectionFacts]:
        """Every detection recorded for one scanned message."""
        ...

    async def repost_review_card(
        self, guild_id: int, detections: list[DetectionFacts]
    ) -> int | None:
        """Post a fresh full card for ``detections``; ``None`` if it could not be posted."""
        ...

    async def set_detection_action(self, guild_id: int, detection_id: int, action: str) -> None: ...
    async def set_detection_hashes(
        self, guild_id: int, detection_id: int, hashes: dict[str, int]
    ) -> None:
        """Backfill hashes onto a detection that was filed without them."""
        ...

    async def rest_delete_message(self, channel_id: int, message_id: int) -> bool:
        """Best-effort message delete; ``False`` when REST is unavailable/refused."""
        ...

    async def rest_ban(
        self, guild_id: int, user_id: int, *, reason: str, purge_seconds: int
    ) -> bool: ...
    async def rest_unban(self, guild_id: int, user_id: int, *, reason: str) -> bool: ...
    async def rest_attachment_url(
        self, channel_id: int, message_id: int, attachment_id: int
    ) -> str | None: ...
    async def rest_create_review_channel(
        self, guild_id: int, *, name: str, mod_role_ids: list[int]
    ) -> ChannelCreation:
        """Create the private review channel, or say why Discord refused."""
        ...

    async def disable_safe_mode(self, guild_id: int) -> None: ...
    async def local_hash(self, guild_id: int, hash_id: str) -> GuildHash | None: ...
    async def enforcement_blocked(
        self, guild_id: int, channel_id: int, *, action: str, locale: str
    ) -> str | None:
        """Why enforcement in this channel would be refused, or ``None``."""
        ...

    async def access_report(self, guild_id: int) -> AccessReport | None:
        """Per-channel enforcement access for the whole guild, or ``None``."""
        ...

    async def has_pending_scan(self, guild_id: int) -> bool:
        """True when detections are queued waiting for a review channel to be linked."""
        ...

    async def hash_rate_ok(self, user_id: int) -> bool: ...
    async def report_rate_ok(self, user_id: int) -> bool: ...
    async def appeal_cooldown_ok(self, user_id: int) -> bool: ...
    async def audit(
        self, guild_id: int, actor_id: int, action: str, *, target: str | None = None
    ) -> None: ...

    async def is_trusted_guild(self, guild_id: int) -> bool:
        """Whether this guild is on the owner-managed global contributor allowlist."""
        ...

    async def trust_guild(self, guild_id: int, *, added_by: int) -> bool:
        """Approve a guild for global contribution; ``False`` if already approved."""
        ...

    async def untrust_guild(self, guild_id: int) -> bool:
        """Remove a guild from the contributor allowlist; ``False`` if absent."""
        ...

    async def list_trusted_guilds(self) -> list[int]:
        """Ids of all approved contributor guilds, oldest first."""
        ...

    async def link_guilds(self, guild_id: int, other_id: int, *, added_by: int) -> LinkResult:
        """Link two servers so they keep one blocklist; copies entries both ways."""
        ...

    async def unlink_guild(self, guild_id: int) -> bool:
        """Take a server out of its link group; ``False`` if it was not linked."""
        ...

    async def list_links(self) -> list[list[int]]:
        """Every link group, as lists of server ids."""
        ...

    async def global_vote(
        self,
        *,
        hash_id: str,
        phash: int,
        dhash: int,
        whash: int,
        voter_user_id: int,
        voter_guild_id: int,
    ) -> str | None:
        """Record one server's Confirm as a global promotion vote.

        Creates the candidate if this is the first vote, then records the
        approval. Returns ``"promoted"`` when this vote met the promotion bar
        (distinct moderators in distinct approved servers), ``"candidate"``
        when the vote was recorded but the bar is not met yet, or ``None``
        when the vote was refused (rate limit, reputation) — refusal never
        fails the local confirm, which has already happened.
        """
        ...

    async def global_dispute(self, hash_id: str) -> bool:
        """Revoke a global hash after a local False-positive verdict.

        Returns ``True`` when a global candidate/promoted entry existed and
        was revoked (docking the submitter's reputation), ``False`` when the
        hash was never global — the common case for purely local detections.
        """
        ...

    async def rest_owner_ids(self) -> set[int]:
        """User ids allowed to run owner commands (application owner / team).

        Empty set when the lookup fails — owner commands then refuse, which is
        the safe default.
        """
        ...

    async def compute_attachment_hashes(self, *, attachment_id: int, url: str) -> AttachmentHashes:
        """Fetch and decode one attachment and compute its hash set. No DB access.

        Deliberately split out from storing the result: this does a network
        fetch plus a sandboxed decode subprocess, both of which can take real
        wall-clock time (multi-second on a loaded host) and must never run
        while a DB write transaction is open -- SQLite holds an exclusive
        file-level write lock for the full lifetime of the transaction, and a
        review of a multi-image message previously ran every attachment's
        fetch+decode one after another *inside* the same open transaction as
        the DB writes, which could hold that lock far longer than any normal
        query and starve concurrent writers into a "database is locked" error.

        Raises :class:`AttachmentHashError` (see
        :mod:`optimus.services.interactions.attachment_hash`) if the attachment
        cannot be fetched or decoded as an image; the caller decides how to
        surface that (skip-and-continue for a multi-image review).
        """
        ...

    async def store_attachment_hash(
        self, guild_id: int, *, hashes: AttachmentHashes, added_by: int
    ) -> GuildHash:
        """Store an already-computed attachment hash set as a guild hash.

        DB-only -- no network or decode work happens here, so this is always
        fast and holds the session's write lock only as long as one insert
        takes. If a hash with the same id already exists for this guild (e.g.
        re-reviewing a message, or an image an earlier detection already
        caught), returns the existing row rather than raising -- adding a scam
        hash is idempotent.
        """
        ...

    async def submit_confirmed_scam(
        self,
        guild_id: int,
        *,
        channel_id: int,
        message_id: int,
        attachment_id: int,
        uploader_id: int,
        matched_hash_id: str,
        confirmed_by: int | None = None,
        review_card_id: int | None = None,
        whitelist_removed: int = 0,
    ) -> None:
        """Record a moderator-confirmed scam match and run the moderation pipeline.

        Feeds the same ``verdict.v1`` path a live detection would, so the
        guild's configured ``action_policy`` (e.g. delete + ban) is applied
        exactly as it would be for a message caught in real time.
        """
        ...

    async def submit_user_report(
        self,
        guild_id: int,
        *,
        channel_id: int,
        message_id: int,
        attachment_id: int,
        attachment_url: str,
        uploader_id: int,
        reporter_id: int,
    ) -> None:
        """File a member's scam report into the mod-review queue.

        Never stores a hash and never auto-acts -- it only surfaces a review
        card (with the reporter attributed) for moderators to decide on.
        Deduplicated per reported message.
        """
        ...


def _require(ctx: InteractionContext, permission: Permission | None) -> None:
    """Enforce guild-only + server-side permission, raising on failure."""
    if permission is not None and ctx.guild_id is None:
        raise InteractionRejected(CommandError.GUILD_ONLY)
    if permission is not None and not has_permission(ctx.member_permissions, permission):
        raise InteractionRejected(CommandError.NO_PERMISSION)


async def handle_command(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    """Dispatch a slash command to its handler after the auth gate."""
    also = ALSO_ACCEPTED.get(ctx.command)
    if (
        also is not None
        and ctx.guild_id is not None
        and has_permission(ctx.member_permissions, also)
    ):
        pass  # e.g. Manage Server still opens a command moved to Manage Messages
    else:
        _require(ctx, required_permission(ctx.command))
    handler = _COMMAND_HANDLERS.get(ctx.command)
    if handler is None:  # pragma: no cover - registration guarantees coverage
        raise InteractionRejected(CommandError.UNKNOWN_FIELD)
    return await handler(ctx, deps)


async def _cmd_scamhash(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    assert ctx.guild_id is not None  # guaranteed by _require (any permission => guild-only)
    sub = ctx.subcommand
    if sub == "add":
        return await _scamhash_add(ctx, deps)
    if sub == "remove":
        hash_id = str(ctx.options["hash_id"])
        removed = await deps.remove_guild_hash(ctx.guild_id, hash_id)
        if removed == 0:
            return InteractionResponse("command.hash_not_found", {"hash_id": hash_id})
        await deps.audit(ctx.guild_id, ctx.user_id, "scamhash.remove", target=hash_id)
        return InteractionResponse("command.hash_removed", {"hash_id": hash_id})
    if sub == "list":
        rows = await deps.list_guild_hashes(ctx.guild_id)
        if not rows:
            return InteractionResponse("command.hash_list_empty")
        shown = sorted(rows, key=_added_at, reverse=True)[:_HASH_LIST_PREVIEW_LIMIT]
        params: dict[str, Any] = {
            "count": len(rows),
            "shown": len(shown),
            "hashes": "\n".join(_render_hash_entry(r) for r in shown),
        }
        if len(rows) <= _HASH_LIST_PREVIEW_LIMIT:
            return InteractionResponse("command.hash_list_header", params)
        return InteractionResponse("command.hash_list_truncated", params)
    if sub == "import":
        raw = ctx.options.get("file")
        # The glue replaces Discord's attachment id with the downloaded bytes.
        # Anything else (an unresolved id, a missing option) must never reach
        # the parser -- a bare number is valid JSON and would be misreported
        # as a malformed export.
        if not isinstance(raw, (str, bytes)):
            raise InteractionRejected(CommandError.IMPORT_DOWNLOAD_FAILED)
        entries = validate_import(raw)
        added = await _import_hashes(deps, ctx.guild_id, entries, added_by=ctx.user_id)
        await deps.audit(ctx.guild_id, ctx.user_id, "scamhash.import", target=str(added))
        return InteractionResponse(
            "command.import_ok", {"added": added, "skipped": len(entries) - added}
        )
    if sub == "export":
        rows = await deps.list_guild_hashes(ctx.guild_id)
        whitelist = await deps.list_whitelist(ctx.guild_id)
        if not rows and not whitelist:
            return InteractionResponse("command.export_empty")
        body = build_export(
            [_ImportHash(phash=r.phash, dhash=r.dhash, whash=r.whash) for r in rows],
            whitelist=[_whitelist_export_row(w) for w in whitelist],
        )
        return InteractionResponse(
            "command.export_ok",
            {"count": len(rows), "whitelisted": len(whitelist)},
            attachment=body,
        )
    if sub == "review":
        return await _review_message(ctx, deps)
    if sub == "whitelist":
        return await _scamhash_whitelist(ctx, deps)
    if sub == "unwhitelist":
        return await _scamhash_unwhitelist(ctx, deps)
    raise InteractionRejected(CommandError.UNKNOWN_FIELD)  # pragma: no cover


#: What a single problem answers when nothing at all could be blocked:
#: (i18n key, or the rejection whose existing text already says it).
_ADD_ONLY_PROBLEM: dict[AddProblem, str | CommandError] = {
    AddProblem.NOT_IMAGE: "command.add_not_image",
    AddProblem.MESSAGE_NO_IMAGES: "command.add_message_no_images",
    AddProblem.MESSAGE_NOT_FOUND: CommandError.MESSAGE_NOT_FOUND,
    AddProblem.MESSAGE_UNREADABLE: CommandError.FETCH_FAILED,
    AddProblem.MESSAGE_OTHER_SERVER: "command.add_message_other_server",
    AddProblem.BAD_URL: "command.add_bad_url",
}


def _add_notes(problems: list[AddProblem], failed: int, locale: str) -> list[str]:
    """One short line per input that was skipped, plus any failed downloads."""
    lines = [translate(f"command.add_skip_{p.value}", locale) for p in problems]
    if failed:
        lines.append(translate("command.add_fetch_failed_count", locale, count=failed))
    return lines


async def _scamhash_add(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    """Block every image the ``image``/``message``/``url`` inputs resolved to.

    Each input stands alone: a problem with one is reported next to what the
    others blocked, never instead of it. Only when nothing at all could be
    blocked does a single problem get its own specific answer.

    An image this server's blocklist already covers -- the same image, or a
    copy the scanner already flags with confidence -- is not stored again:
    the reply names the entry that covers it, and no audit row is written.
    Whitelist entries that cover the image are removed first (the reply
    names them), so the block takes effect.
    """
    assert ctx.guild_id is not None
    if not await deps.hash_rate_ok(ctx.user_id):
        raise InteractionRejected(CommandError.RATE_LIMITED)
    images = [(int(a), str(u)) for a, u in ctx.options.get("images") or []]
    problems = [AddProblem(p) for p in ctx.options.get("problems") or []]
    # Every download first, then every write: fetching must not run inside
    # the transaction (see compute_attachment_hashes).
    computed: list[AttachmentHashes] = []
    failures: list[str] = []
    for attachment_id, url in images:
        try:
            computed.append(
                await deps.compute_attachment_hashes(attachment_id=attachment_id, url=url)
            )
        except AttachmentHashError as exc:
            failures.append(str(exc))

    if not computed:
        if failures and not problems:
            return InteractionResponse("command.add_fetch_failed", {"reason": failures[0]})
        if len(problems) == 1 and not failures:
            answer = _ADD_ONLY_PROBLEM[problems[0]]
            if isinstance(answer, CommandError):
                raise InteractionRejected(answer)
            return InteractionResponse(answer)
        if not problems:
            # No input given at all.
            return InteractionResponse("command.add_not_image")
        notes = _add_notes(problems, len(failures), ctx.locale)
        return InteractionResponse("command.add_nothing_blocked", {"notes": "\n".join(notes)})

    blocked: list[str] = []
    known_lines: list[str] = []
    whitelist_lines: list[str] = []
    # A moderator blocking an image means it is a scam: a whitelist entry
    # that covers it (a misclicked False positive) would make the block
    # useless, because the whitelist wins. Lift those entries first.
    lifted = await _lift_whitelist(
        deps, ctx.guild_id, ctx.user_id, [h.phash for h in computed], cause="scamhash add"
    )
    if lifted:
        whitelist_lines.append(
            translate("command.add_unwhitelisted", ctx.locale, entries=_entry_refs(lifted))
        )
    for hashes in computed:
        known = await deps.known_image(ctx.guild_id, hashes)
        if known.entry is not None:
            if known.entry.hash_id not in blocked:  # not just added by this command
                key = "command.add_already_listed" if known.exact else "command.add_already_caught"
                known_lines.append(
                    translate(
                        key,
                        ctx.locale,
                        hash_id=known.entry.hash_id,
                        origin=_hash_origin(known.entry),
                    )
                )
            continue
        stored = await deps.add_guild_hash(ctx.guild_id, _hashes_to_guild_hash(hashes, ctx.user_id))
        if stored.hash_id not in blocked:
            blocked.append(stored.hash_id)
            await deps.audit(ctx.guild_id, ctx.user_id, "scamhash.add", target=stored.hash_id)
    notes = known_lines + whitelist_lines + _add_notes(problems, len(failures), ctx.locale)
    if not blocked:
        if len(notes) == 1 and known_lines:
            return InteractionResponse("command.add_known", {"notes": notes[0]})
        return InteractionResponse("command.add_nothing_blocked", {"notes": "\n".join(notes)})
    if len(images) == 1 and not notes:
        return InteractionResponse("command.hash_added", {"hash_id": blocked[0]})
    return InteractionResponse(
        "command.hashes_added",
        {
            "count": len(blocked),
            "hash_ids": ", ".join(f"`{h}`" for h in blocked),
            "notes": "".join(f"\n{line}" for line in notes),
        },
    )


#: How each blocklist entry got there, in the words moderators know.
_HASH_SOURCE_LABELS: dict[str, str] = {
    "local": "/scamhash add",
    "review_confirm": "Confirm scam",
    "reviewmsg": "Review as scam",
    "campaign_sweep": "campaign cleanup",
    "import": "import",
}


def _added_at(row: GuildHash) -> datetime:
    """When a blocklist entry was added, as an aware UTC datetime.

    SQLite hands timestamps back naive; they are stored in UTC.
    """
    when = row.created_at
    if when is None:
        return datetime.min.replace(tzinfo=UTC)
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _who(user_id: int) -> str:
    """A mention for a moderator; "Optimus" for the bot's own actions.

    The campaign cleanup stores :data:`SYSTEM_ACTOR` (0) as the author, and
    ``<@0>`` renders as a broken mention.
    """
    return "Optimus" if user_id == SYSTEM_ACTOR else f"<@{user_id}>"


def _hash_origin(row: GuildHash) -> str:
    """How a blocklist entry got there, by whom, and when: ``Confirm scam by @x <date>``."""
    source = _HASH_SOURCE_LABELS.get(row.source, row.source)
    added_by = f" by {_who(row.added_by)}" if row.added_by is not None else ""
    when = f" <t:{int(_added_at(row).timestamp())}:d>" if row.created_at is not None else ""
    return f"{source}{added_by}{when}"


def _render_hash_entry(row: GuildHash) -> str:
    """One line per hash: id, how it was added, by whom, and when."""
    return f"\u2022 `{row.hash_id}` \u2014 {_hash_origin(row)}"


# --- whitelist management ----------------------------------------------------------

#: ``/scamhash whitelist`` page size: 10 entries keep a page well inside
#: Discord's 2,000-character message limit.
WHITELIST_PAGE_SIZE = 10
#: Hash ids a whitelist line names before it says "+N more".
_COVERS_SHOWN = 3
#: Entries the ``by``/``since`` preview names before it says "+N more".
_PREVIEW_SHOWN = 20
#: ``since`` units and their length.
_SINCE_UNITS: dict[str, timedelta] = {
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}
#: Longest ``since`` window accepted.
_SINCE_MAX = timedelta(days=365)


def _covers(entry_phash: int, image_phash: int) -> bool:
    """Whether a whitelist entry exempts an image -- the scanner's own test."""
    return (entry_phash ^ image_phash).bit_count() <= DEFAULT_WHITELIST_RADIUS


def _entry_refs(rows: Sequence[GuildWhitelist]) -> str:
    """``#40, #41`` -- the numbers moderators pass to ``/scamhash unwhitelist``."""
    return ", ".join(f"#{r.id}" for r in rows)


def _whitelist_added_at(row: GuildWhitelist) -> datetime:
    """When a whitelist entry was added, as an aware UTC datetime."""
    when = row.created_at
    if when is None:
        return datetime.min.replace(tzinfo=UTC)
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


async def _lift_whitelist(
    deps: InteractionDeps,
    guild_id: int,
    user_id: int,
    phashes: Sequence[int],
    *,
    cause: str,
) -> list[GuildWhitelist]:
    """Remove every whitelist entry that covers one of ``phashes``.

    Used when a moderator calls an image a scam: an entry that still covers it
    would win over the blocklist and keep the scanner quiet. Returns the
    removed entries; each removal gets a ``scamhash.unwhitelist`` audit row.
    """
    if not phashes:
        return []
    rows = [
        w for w in await deps.list_whitelist(guild_id) if any(_covers(w.phash, p) for p in phashes)
    ]
    if not rows:
        return []
    await deps.remove_whitelist(guild_id, [int(r.id) for r in rows])
    for row in rows:
        await deps.audit(guild_id, user_id, "scamhash.unwhitelist", target=f"#{row.id} ({cause})")
    return rows


def _whitelist_reason(row: GuildWhitelist, locale: str) -> str:
    """Why an entry exists, in the words of the button that created it."""
    reason = row.reason or ""
    for prefix, action in (
        ("false positive: detection #", ReviewAction.FALSE_POSITIVE),
        ("review: detection #", ReviewAction.WHITELIST_IMAGE),
    ):
        if reason.startswith(prefix):
            return translate(
                "command.whitelist_reason_card",
                locale,
                action=BUTTON_LABELS[action],
                detection_id=reason[len(prefix) :],
            )
    return reason or translate("command.whitelist_reason_unknown", locale)


def _render_whitelist_entry(
    row: GuildWhitelist, blocklist: Sequence[GuildHash], locale: str
) -> str:
    """One line per entry: number, why, who, when, and what it overrides."""
    line = f"\u2022 **#{row.id}** \u2014 {_whitelist_reason(row, locale)}"
    if row.added_by is not None:
        line += f" by {_who(row.added_by)}"
    if row.created_at is not None:
        line += f" <t:{int(_whitelist_added_at(row).timestamp())}:d>"
    covered = [h.hash_id for h in blocklist if _covers(row.phash, h.phash)]
    if covered:
        shown = ", ".join(f"`{h}`" for h in covered[:_COVERS_SHOWN])
        more = len(covered) - _COVERS_SHOWN
        line += "\n  " + translate(
            "command.whitelist_overrides" if more <= 0 else "command.whitelist_overrides_more",
            locale,
            hash_ids=shown,
            more=more,
        )
    return line


def _parse_since(raw: Any) -> timedelta | None:
    """``30m`` / ``2h`` / ``3d`` / ``1w`` -> a window; ``None`` when not given.

    Raises :class:`InteractionRejected` for anything else, or a window longer
    than a year.
    """
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip().lower()
    unit = _SINCE_UNITS.get(text[-1:])
    if unit is None or not text[:-1].isdigit() or int(text[:-1]) <= 0:
        raise InteractionRejected(CommandError.BAD_SINCE)
    window = unit * int(text[:-1])
    if window > _SINCE_MAX:
        raise InteractionRejected(CommandError.BAD_SINCE)
    return window


def _filter_whitelist(
    rows: Sequence[GuildWhitelist], by: int | None, since: timedelta | None
) -> list[GuildWhitelist]:
    """Entries added by ``by`` within ``since``, newest first."""
    cutoff = datetime.now(UTC) - since if since is not None else None
    picked = [
        r
        for r in rows
        if (by is None or r.added_by == by) and (cutoff is None or _whitelist_added_at(r) >= cutoff)
    ]
    return sorted(picked, key=lambda r: (_whitelist_added_at(r), r.id or 0), reverse=True)


def _filter_text(by: int | None, since: Any, locale: str) -> str:
    """`` added by @x in the last 2h`` -- echoes the filter back, or ``""``."""
    parts: list[str] = []
    if by is not None:
        parts.append(translate("command.whitelist_filter_by", locale, user_id=by))
    if since is not None and str(since).strip():
        parts.append(translate("command.whitelist_filter_since", locale, since=str(since).strip()))
    return "".join(f" {p}" for p in parts)


def _whitelist_export_row(row: GuildWhitelist) -> dict[str, Any]:
    """One whitelist entry in the export file (read by people, not by import)."""
    return {
        "entry": row.id,
        "phash": f"{row.phash:016x}",
        "reason": row.reason,
        "added_by": row.added_by,
        "created_at": _whitelist_added_at(row).isoformat() if row.created_at else None,
    }


async def _scamhash_whitelist(
    ctx: InteractionContext, deps: InteractionDeps
) -> InteractionResponse:
    """``/scamhash whitelist [page] [by] [since]`` -- what the whitelist exempts.

    Each line shows the entry number ``/scamhash unwhitelist`` takes, which
    button created it, who and when, and the blocklist hashes it overrides
    (the whitelist wins over them).
    """
    assert ctx.guild_id is not None
    by = int(ctx.options["by"]) if ctx.options.get("by") is not None else None
    since_raw = ctx.options.get("since")
    rows = _filter_whitelist(await deps.list_whitelist(ctx.guild_id), by, _parse_since(since_raw))
    filtered = _filter_text(by, since_raw, ctx.locale)
    if not rows:
        key = "command.whitelist_none_match" if filtered else "command.whitelist_empty"
        return InteractionResponse(key, {"filter": filtered})
    pages = math.ceil(len(rows) / WHITELIST_PAGE_SIZE)
    page = min(max(int(ctx.options.get("page") or 1), 1), pages)
    shown = rows[(page - 1) * WHITELIST_PAGE_SIZE : page * WHITELIST_PAGE_SIZE]
    blocklist = await deps.list_guild_hashes(ctx.guild_id)
    return InteractionResponse(
        "command.whitelist_page",
        {
            "count": len(rows),
            "filter": filtered,
            "page": page,
            "pages": pages,
            "entries": "\n".join(_render_whitelist_entry(r, blocklist, ctx.locale) for r in shown),
        },
    )


def _parse_entry_refs(raw: str) -> tuple[set[int], set[int], list[str]]:
    """Split ``entry:`` into entry numbers, image hashes, and unreadable tokens.

    ``12`` or ``#12`` is an entry number; a 16-character hex id (as
    ``/scamhash list`` and the add replies show it) is an image hash.
    """
    numbers: set[int] = set()
    hashes: set[int] = set()
    bad: list[str] = []
    for token in raw.replace(",", " ").split():
        bare = token.strip("`").removeprefix("#")
        if len(bare) == 16 and all(c in "0123456789abcdefABCDEF" for c in bare):
            hashes.add(int(bare, 16))
        elif bare.isdigit() and len(bare) < 16:
            numbers.add(int(bare))
        else:
            bad.append(token)
    return numbers, hashes, bad


async def _remove_entries(
    ctx: InteractionContext, deps: InteractionDeps, rows: Sequence[GuildWhitelist]
) -> None:
    assert ctx.guild_id is not None
    await deps.remove_whitelist(ctx.guild_id, [int(r.id) for r in rows])
    for row in rows:
        await deps.audit(ctx.guild_id, ctx.user_id, "scamhash.unwhitelist", target=f"#{row.id}")


async def _scamhash_unwhitelist(
    ctx: InteractionContext, deps: InteractionDeps
) -> InteractionResponse:
    """``/scamhash unwhitelist`` -- remove entries so Optimus flags the images again.

    ``entry:`` names entries (numbers or image hashes) and removes them at
    once. ``by:`` / ``since:`` select a batch -- a run of misclicks -- and only
    preview it; the same command with ``confirm:True`` removes it. Nothing is
    kept between the two runs, so the preview cannot go stale in storage.
    """
    assert ctx.guild_id is not None
    entry_raw = str(ctx.options.get("entry") or "").strip()
    by = int(ctx.options["by"]) if ctx.options.get("by") is not None else None
    since_raw = ctx.options.get("since")
    since = _parse_since(since_raw)
    if entry_raw and (by is not None or since is not None):
        return InteractionResponse("command.unwhitelist_entry_or_filter")
    if not entry_raw and by is None and since is None:
        return InteractionResponse("command.unwhitelist_nothing_given")
    rows = await deps.list_whitelist(ctx.guild_id)

    if entry_raw:
        numbers, hashes, bad = _parse_entry_refs(entry_raw)
        picked = [r for r in rows if r.id in numbers or any(_covers(r.phash, h) for h in hashes)]
        found_numbers = {r.id for r in picked}
        missing = [f"#{n}" for n in sorted(numbers - found_numbers)]
        missing += [
            f"`{h:016x}`" for h in sorted(hashes) if not any(_covers(r.phash, h) for r in picked)
        ]
        missing += bad
        if not picked:
            return InteractionResponse(
                "command.unwhitelist_not_found", {"entries": ", ".join(missing) or entry_raw}
            )
        await _remove_entries(ctx, deps, picked)
        notes = (
            "\n" + translate("command.unwhitelist_missing", ctx.locale, entries=", ".join(missing))
            if missing
            else ""
        )
        return InteractionResponse(
            "command.unwhitelist_done",
            {"count": len(picked), "entries": _entry_refs(picked), "notes": notes},
        )

    picked = _filter_whitelist(rows, by, since)
    filtered = _filter_text(by, since_raw, ctx.locale)
    if not picked:
        return InteractionResponse("command.whitelist_none_match", {"filter": filtered})
    if not bool(ctx.options.get("confirm")):
        refs = _entry_refs(picked[:_PREVIEW_SHOWN])
        if len(picked) > _PREVIEW_SHOWN:
            refs += translate(
                "command.unwhitelist_preview_more", ctx.locale, more=len(picked) - _PREVIEW_SHOWN
            )
        return InteractionResponse(
            "command.unwhitelist_preview",
            {"count": len(picked), "filter": filtered, "entries": refs},
        )
    await _remove_entries(ctx, deps, picked)
    return InteractionResponse(
        "command.unwhitelist_done",
        {"count": len(picked), "entries": _entry_refs(picked[:_PREVIEW_SHOWN]), "notes": ""},
    )


async def _cmd_report_message(
    ctx: InteractionContext, deps: InteractionDeps
) -> InteractionResponse:
    """Entry point for both member-facing report surfaces.

    Serves the "Report scam to mods" right-click context menu and the
    ``/report message:<link-or-id>`` slash command -- the two differ only in
    how the target message reaches ``ctx.options`` (Discord's resolved data
    for the menu, an explicit REST fetch for the slash command), and both
    arrive here with the same pre-resolved ``channel_id`` / ``message_id`` /
    ``author_id`` / ``attachments`` shape.

    Open to every member (no permission gate), so it is deliberately inert:
    it files the message into the mod-review queue and nothing else. No hash
    is stored, nothing is deleted, nobody is actioned -- a hostile member
    mass-reporting innocent messages can, at worst, put cards in front of the
    mods (bounded by a tight per-user rate limit and per-message dedupe).
    """
    if ctx.guild_id is None:
        raise InteractionRejected(CommandError.GUILD_ONLY)
    if not await deps.report_rate_ok(ctx.user_id):
        raise InteractionRejected(CommandError.RATE_LIMITED)
    attachments: list[tuple[int, str]] = list(ctx.options["attachments"])
    if not attachments:
        return InteractionResponse("command.report_no_images")
    message_id = int(ctx.options["message_id"])
    attachment_id, attachment_url = attachments[0]
    await deps.submit_user_report(
        ctx.guild_id,
        channel_id=int(ctx.options["channel_id"]),
        message_id=message_id,
        attachment_id=attachment_id,
        # Carried so the review card can show the reported image. Nothing is
        # deleted by a member report, so the URL is still live when the
        # moderator opens the card.
        attachment_url=attachment_url,
        uploader_id=int(ctx.options["author_id"]),
        reporter_id=ctx.user_id,
    )
    await deps.audit(ctx.guild_id, ctx.user_id, "report.message", target=str(message_id))
    return InteractionResponse("command.report_ok")


async def _cmd_review_message(
    ctx: InteractionContext, deps: InteractionDeps
) -> InteractionResponse:
    """Entry point for the "Review as scam" message context-menu command.

    ``required_permission("review_message")`` gates this the same as
    ``/scamhash review`` (Manage Messages, or Manage Server); the glue layer has already
    resolved the target message's attachments/author into ``ctx.options``
    since a context-menu command carries no typed options of its own.
    """
    return await _review_message(ctx, deps)


async def _review_message(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    """Shared core for both the ``/scamhash review`` and context-menu entry points.

    Expects the glue layer to have pre-resolved the target message into
    ``ctx.options``: ``channel_id``, ``message_id``, ``author_id`` (all ints),
    and ``attachments`` (a list of ``(attachment_id, url)`` pairs already
    filtered to image content types). Hashes every attachment, adds each as a
    new guild hash, and -- for each one successfully hashed -- feeds a
    confirmed-scam verdict into the moderation pipeline so the configured
    action policy (e.g. delete + ban) is applied. A REST-level failure to
    fetch/decode one attachment is skipped rather than aborting the whole
    review, since a message can carry several images and a moderator's intent
    is best served by processing every image that *can* be processed.

    Runs in two passes deliberately: first every attachment is fetched and
    hashed with no DB session/transaction involved at all, then (only once
    all the slow network/decode work is done) each successfully hashed
    attachment is stored and submitted in quick DB-only calls. The whole
    handler still executes inside one caller-managed transaction (see
    :meth:`InteractionService._run`), so interleaving a network fetch plus a
    sandboxed decode subprocess for attachment N+1 with attachment N's writes
    used to hold that transaction's SQLite write lock open for as long as an
    entire multi-image review took -- multiple seconds per image, easily
    exceeding the point another writer would report "database is locked".
    Doing all the slow work up front means the transaction's write lock is
    only ever held for the sum of the fast DB calls, not the fetch+decode
    time too.
    """
    assert ctx.guild_id is not None  # guaranteed by _require (any permission => guild-only)
    if not await deps.hash_rate_ok(ctx.user_id):
        raise InteractionRejected(CommandError.RATE_LIMITED)
    channel_id = int(ctx.options["channel_id"])
    message_id = int(ctx.options["message_id"])
    author_id = int(ctx.options["author_id"])
    attachments: list[tuple[int, str]] = list(ctx.options["attachments"])
    if not attachments:
        return InteractionResponse("command.reviewmsg_no_images")

    # Pass 1: fetch + decode + hash every attachment. Pure computation plus
    # network I/O -- deliberately kept outside any DB write below so the
    # transaction the caller already has open never sits idle waiting on a
    # CDN round-trip or a sandboxed decode subprocess.
    computed: list[tuple[int, AttachmentHashes]] = []
    failed = 0
    for attachment_id, url in attachments:
        try:
            hashes = await deps.compute_attachment_hashes(attachment_id=attachment_id, url=url)
        except AttachmentHashError as exc:
            _log.warning(
                "reviewmsg_attachment_hash_failed",
                guild_id=ctx.guild_id,
                attachment_id=attachment_id,
                reason=str(exc),
            )
            failed += 1
            continue
        computed.append((attachment_id, hashes))

    # Pass 2: store + audit + submit. DB-only, no network/decode work, so
    # each iteration is fast and the write lock is held for close to the
    # minimum time actually needed.
    added_hash_ids: list[str] = []
    lifted = await _lift_whitelist(
        deps,
        ctx.guild_id,
        ctx.user_id,
        [h.phash for _a, h in computed],
        cause="Review as scam",
    )
    for attachment_id, hashes in computed:
        stored = await deps.store_attachment_hash(ctx.guild_id, hashes=hashes, added_by=ctx.user_id)
        added_hash_ids.append(stored.hash_id)
        await deps.audit(ctx.guild_id, ctx.user_id, "scamhash.reviewmsg", target=stored.hash_id)
        await deps.submit_confirmed_scam(
            ctx.guild_id,
            channel_id=channel_id,
            message_id=message_id,
            attachment_id=attachment_id,
            uploader_id=author_id,
            matched_hash_id=stored.hash_id,
            # A moderator's own call: its card is posted already folded.
            confirmed_by=ctx.user_id,
            whitelist_removed=len(lifted),
        )
    if not added_hash_ids:
        return InteractionResponse("command.reviewmsg_all_failed", {"failed": failed})

    # Report what the moderation pipeline will actually do with the submitted
    # verdicts, mirroring optimus.services.moderation.policy.decide for a
    # confirmed verdict (SCAM, confidence 1.0 -- always clears the auto-act
    # bar): safe mode and a report_only/none policy both mean "report only,
    # nothing deleted". The old unconditional "actioned <@user>" reply told a
    # moderator the message was handled even when the configured policy meant
    # the bot would deliberately do nothing to it.
    config = await deps.get_config(ctx.guild_id)
    policy = str(config.get("action_policy") or "report_only")
    params: dict[str, Any] = {
        "added": len(added_hash_ids),
        "failed": failed,
        "author_id": author_id,
        "action": policy,
    }
    if bool(config.get("safe_mode", False)):
        return InteractionResponse("command.reviewmsg_result_safe_mode", params)
    if policy in ("none", "report_only"):
        return InteractionResponse("command.reviewmsg_result_report_only", params)
    # Enforcement runs asynchronously, so "submitted" is all this reply can
    # honestly promise -- unless the bot's own permissions already rule the
    # action out, which is knowable now and is exactly the case that produced
    # a cheerful "actioned" reply followed by nothing happening. Saying it here
    # puts the fix in front of the moderator while they are still looking.
    blocked = await deps.enforcement_blocked(
        ctx.guild_id, channel_id, action=policy, locale=ctx.locale
    )
    review_channel = config.get("review_channel")
    if blocked is not None:
        params["problem"] = blocked
        return InteractionResponse("command.reviewmsg_result_blocked", params)
    if review_channel is None:
        return InteractionResponse("command.reviewmsg_result_submitted_no_channel", params)
    params["review_channel"] = review_channel
    return InteractionResponse("command.reviewmsg_result_submitted", params)


async def _cmd_config(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    assert ctx.guild_id is not None
    if ctx.subcommand == "view":
        current = await deps.get_config(ctx.guild_id)
        locale = str(current.get("locale", "en"))
        summary = _render_config_summary(current, locale)
        # Show a pending-scan notice when the guild joined before ``/setup``
        # linked a review channel and detections have been queued. Kept below
        # the config block so it reads as "here is your config, and by the
        # way you have not run setup yet" rather than crowding the top.
        if current.get("review_channel") is None and await deps.has_pending_scan(ctx.guild_id):
            summary = summary + "\n\n-# " + translate("command.setup_pending_scan", locale)
        return InteractionResponse("command.config_view_header", {"summary": summary})
    if ctx.subcommand == "permissions":
        current = await deps.get_config(ctx.guild_id)
        locale = str(current.get("locale", ctx.locale))
        report = await deps.access_report(ctx.guild_id)
        # No report means "could not check" (cache not warm, no gateway cache
        # wired), which must not be shown as a clean bill of health.
        if report is None:
            return InteractionResponse("command.permissions_unknown")
        return InteractionResponse(
            "command.permissions_report", {"report": explain_access_report(report, locale)}
        )
    change = validate_config_set(str(ctx.options["field"]), str(ctx.options["value"]))
    if change.field == "mod_queue_threshold":
        ceiling = deps.auto_act_threshold()
        if change.value > ceiling:
            # Above the auto-act bar the policy engine has no valid ordering and
            # used to raise on every image, silently switching detection off for
            # the whole server. Refuse it here, naming the limit, so a cautious
            # moderator gets an answer instead of an outage.
            return InteractionResponse(
                "command.config_threshold_too_high",
                {"value": f"{change.value:g}", "max": f"{ceiling:g}"},
            )
    await deps.set_config_field(ctx.guild_id, change.field, change.value)
    await deps.audit(ctx.guild_id, ctx.user_id, "config.set", target=change.field)
    return InteractionResponse(
        "command.config_set_ok",
        {"field": change.field, "value": _render_config_value(change.field, change.value)},
    )


#: Display order for /config view; keeps related settings grouped together.
#: These keys must exactly match both the dict keys returned by
#: InteractionDeps.get_config() and the field names validate_config_set()
#: accepts for /config set -- i.e. "review_channel", never the DB column's
#: "review_channel_id" -- so a field name copied from /config view always
#: works verbatim in /config set and vice versa.
_CONFIG_VIEW_ORDER = (
    "sensitivity",
    "action_policy",
    "mod_queue_threshold",
    "review_channel",
    "ban_purge_hours",
    "safe_mode",
    "retention_days",
    "locale",
    "optin_global_db",
    "optin_scan_bots",
)


def _render_config_summary(current: dict[str, Any], locale: str = "en") -> str:
    """Render a guild's config dict (from ``get_config``) as a display block.

    Empty (no row yet / guild never configured) renders a single explanatory
    line rather than an empty list. ``review_channel`` renders as a real
    channel mention (or "not set") to match ``_render_config_value``. Each
    field carries its one-line ``config_help.*`` explanation so ``/config
    view`` is self-documenting — mods should not need the manual to know what
    a knob does.
    """
    if not current:
        return "_No configuration set yet \u2014 defaults are in effect._"
    lines = []
    for config_field in _CONFIG_VIEW_ORDER:
        if config_field not in current:
            continue
        value = current[config_field]
        # A channel mention only renders as the channel's name outside a code
        # span -- inside backticks Discord shows the literal "<#123...>", which
        # is exactly the raw id a mod would otherwise have to decode. Every
        # other value stays in backticks: it is the literal text /config set
        # accepts.
        if config_field == "review_channel":
            lines.append(
                f"**{config_field}**: " + (f"<#{value}>" if value is not None else "`not set`")
            )
            lines.append(f"-# {translate(f'config_help.{config_field}', locale)}")
            continue
        rendered = str(value)
        lines.append(f"**{config_field}**: `{rendered}`")
        lines.append(f"-# {translate(f'config_help.{config_field}', locale)}")
    return "\n".join(lines)


def _render_config_value(field: str, value: Any) -> str:
    """Render a validated config value for the ``config_set_ok`` confirmation.

    ``review_channel`` stores a raw channel id (or ``None`` when cleared); show
    it as a real channel mention (or "none") instead of a bare integer/"None".
    Every other field renders as-is.

    The returned string carries its own formatting -- a mention must stay
    *outside* backticks to render as the channel's name, so the catalog template
    interpolates it bare and each field decides how it is quoted.
    """
    if field == "review_channel":
        return f"<#{value}>" if value is not None else "`none`"
    return f"`{value}`"


#: Name of the review channel ``/setup`` creates when none is linked yet.
REVIEW_CHANNEL_NAME = "optimus-review"


async def _cmd_setup(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    """Wire up the shared mod-review channel where detections await approval.

    Three shapes, in priority order:

    - ``channel`` option given: point reviews at that existing channel (also
      how you re-point after a mistake) -- visibility stays whatever the mods
      configured on it, the bot never edits someone else's channel perms.
    - No option, nothing linked yet: create a fresh private ``#optimus-review``
      (deny @everyone, allow the bot + the optional ``mod_role``; admins bypass
      overwrites) and link it.
    - No option, already linked: report the current channel instead of piling
      up duplicate channels on every re-run.
    """
    assert ctx.guild_id is not None
    channel_opt = ctx.options.get("channel")
    if channel_opt is not None:
        channel_id = int(channel_opt)
        await deps.set_config_field(ctx.guild_id, "review_channel", channel_id)
        await deps.audit(ctx.guild_id, ctx.user_id, "setup.review_channel", target=str(channel_id))
        return InteractionResponse("command.setup_linked", {"channel_id": channel_id})
    config = await deps.get_config(ctx.guild_id)
    existing = config.get("review_channel")
    if existing is not None:
        return InteractionResponse("command.setup_already", {"channel_id": existing})
    mod_role = ctx.options.get("mod_role")
    # Network before the transaction's first write (SQLite write-lock discipline).
    created = await deps.rest_create_review_channel(
        ctx.guild_id,
        name=REVIEW_CHANNEL_NAME,
        mod_role_ids=[int(mod_role)] if mod_role is not None else [],
    )
    if created.channel_id is None:
        return _setup_failure_response(created)
    channel_id = created.channel_id
    await deps.set_config_field(ctx.guild_id, "review_channel", channel_id)
    await deps.audit(ctx.guild_id, ctx.user_id, "setup.review_channel", target=str(channel_id))
    key = "command.setup_created" if mod_role is not None else "command.setup_created_no_role"
    return InteractionResponse(key, {"channel_id": channel_id})


def _setup_failure_response(created: ChannelCreation) -> InteractionResponse:
    """Turn a refused creation into advice the moderator can actually act on.

    Falls back to the generic key when the failure is unset or unmapped, so a
    future enum member can never surface a raw ``KeyError`` to a moderator.
    """
    failure = created.failure or SetupFailure.UNKNOWN
    key = SETUP_FAILURE_KEYS.get(failure, "command.setup_failed")
    if failure is SetupFailure.RATE_LIMITED:
        # Round up: "retry in 0 minutes" is worse than no number at all.
        seconds = created.retry_seconds or 0
        return InteractionResponse(key, {"minutes": max(1, math.ceil(seconds / 60))})
    return InteractionResponse(key)


def _stats_load_block(summary: dict[str, Any], locale: str) -> str:
    """Render the pipeline-load section of ``/stats``, or nothing.

    The throughput numbers are bot-wide and reset on restart (see
    :mod:`optimus.core.loadstats`), so the heading says so rather than
    letting a moderator read another server's traffic as their own. The
    skip breakdown is only appended when something was actually skipped:
    on a healthy server it would otherwise be four zeroes of noise on every
    invocation.

    Returns ``""`` when the process has done no work at all -- a bot that
    has just restarted, where every number would be zero and the section
    says nothing a moderator can act on.
    """
    scanned = int(summary.get("scanned", 0))
    queued = int(summary.get("queued", 0))
    skipped = int(summary.get("skipped", 0))
    if scanned == 0 and queued == 0 and skipped == 0:
        return ""
    detail = ""
    if skipped:
        detail = translate(
            "command.stats_load_skipped",
            locale,
            duplicates=f"{int(summary.get('duplicates', 0)):,}",
            rejected=f"{int(summary.get('rejected', 0)):,}",
            rate_limited=f"{int(summary.get('rate_limited', 0)):,}",
            dropped=f"{int(summary.get('dropped', 0)):,}",
        )
    return translate(
        "command.stats_load",
        locale,
        scanned=f"{scanned:,}",
        queued=f"{queued:,}",
        skipped=f"{skipped:,}",
        skipped_detail=detail,
    )


async def _cmd_stats(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    assert ctx.guild_id is not None
    summary = await deps.stats_summary(ctx.guild_id)
    if not summary:
        return InteractionResponse("command.stats_empty")
    # Zero detections still renders the header: the database line doubles as the
    # persistence canary (boot count + stable first-boot date), which moderators
    # need to see on quiet servers too.
    return InteractionResponse(
        "command.stats_header",
        {
            "hours": summary.get("hours", 24),
            "detections": summary.get("detections", 0),
            "boots": summary.get("boots", 0),
            "first_boot": summary.get("first_boot", "unknown"),
            "load": _stats_load_block(summary, ctx.locale),
        },
    )


async def _cmd_global(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    # Owner-only: Discord has no "application owner" permission flag, so the
    # gate is enforced here against the application's owner/team ids. An empty
    # set (lookup failed) refuses — fail closed on the trust-granting command.
    if ctx.user_id not in await deps.rest_owner_ids():
        return InteractionResponse("command.owner_only")
    if ctx.subcommand == "links":
        groups = await deps.list_links()
        if not groups:
            return InteractionResponse("command.global_links_none")
        listing = "\n".join(
            "\u2022 " + " \u2194 ".join(f"`{gid}`" for gid in group) for group in groups
        )
        return InteractionResponse(
            "command.global_links", {"count": len(groups), "listing": listing}
        )
    if ctx.subcommand == "servers":
        ids = await deps.list_trusted_guilds()
        if not ids:
            return InteractionResponse("command.global_servers_none")
        listing = "\n".join(f"\u2022 `{gid}`" for gid in ids)
        return InteractionResponse(
            "command.global_servers", {"count": len(ids), "listing": listing}
        )
    raw = str(ctx.options["server_id"]).strip()
    if not raw.isdigit():
        return InteractionResponse("command.global_invalid_server", {"value": raw})
    server_id = int(raw)
    if ctx.subcommand == "approve_server":
        added = await deps.trust_guild(server_id, added_by=ctx.user_id)
        if ctx.guild_id is not None:
            await deps.audit(ctx.guild_id, ctx.user_id, "global.approve_server", target=raw)
        key = "command.global_server_approved" if added else "command.global_server_already"
        return InteractionResponse(key, {"server_id": raw})
    if ctx.subcommand == "link_server":
        if ctx.guild_id is None:
            raise InteractionRejected(CommandError.GUILD_ONLY)
        if server_id == ctx.guild_id:
            return InteractionResponse("command.global_link_self")
        result = await deps.link_guilds(ctx.guild_id, server_id, added_by=ctx.user_id)
        await deps.audit(ctx.guild_id, ctx.user_id, "global.link_server", target=raw)
        gained = ", ".join(f"`{g}` +{result.gained.get(g, 0)}" for g in result.members)
        return InteractionResponse(
            "command.global_linked",
            {"server_id": raw, "count": len(result.members), "gained": gained},
        )
    if ctx.subcommand == "unlink_server":
        removed = await deps.unlink_guild(server_id)
        if ctx.guild_id is not None:
            await deps.audit(ctx.guild_id, ctx.user_id, "global.unlink_server", target=raw)
        key = "command.global_unlinked" if removed else "command.global_not_linked"
        return InteractionResponse(key, {"server_id": raw})
    if ctx.subcommand == "revoke_server":
        removed = await deps.untrust_guild(server_id)
        if ctx.guild_id is not None:
            await deps.audit(ctx.guild_id, ctx.user_id, "global.revoke_server", target=raw)
        key = "command.global_server_revoked" if removed else "command.global_server_missing"
        return InteractionResponse(key, {"server_id": raw})
    raise InteractionRejected(CommandError.UNKNOWN_FIELD)  # pragma: no cover


#: How many open cards ``/queue`` *fetches*, following the ``/setup`` replay's
#: "show some, count the rest" convention. This bounds the query, not the
#: message: the character budget below decides how many of these actually
#: render, which at ordinary snowflake widths is around half of them.
QUEUE_PAGE_SIZE = 25
#: Discord's hard ceiling on message content. Each row carries two snowflakes
#: and a jump URL, so a full page renders past 3000 characters at ordinary ID
#: widths -- the row cap alone cannot keep the response sendable, and a
#: rejected message would make ``/queue`` fail exactly when the backlog is
#: worst. The budget is measured against the rendered template per locale.
DISCORD_MESSAGE_LIMIT = 2000


def _format_age(seconds: float) -> str:
    """Render an age as the coarsest useful unit (``3d``, ``4h``, ``12m``)."""
    if seconds >= 86400:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 60)}m"


async def _cmd_queue(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    """List review cards nobody has acted on yet, oldest first.

    The cards themselves stay the work surface -- this only answers "what is
    waiting?", which the review channel cannot once the backlog has scrolled
    past a screen or piled up while no moderator was online. Strictly
    read-only: it starts no action and changes no state, so the buttons remain
    the only way to resolve anything.

    Rows with no card posted yet are deliberately excluded (see
    ``DetectionRepository.list_open``) -- those belong to the ``/setup``
    replay, and listing them here would promise a card that does not exist.
    """
    assert ctx.guild_id is not None
    reopen = ctx.options.get("detection")
    if reopen is not None:
        return await _reopen_card(ctx, deps, int(reopen))
    summary = await deps.open_queue(ctx.guild_id, limit=QUEUE_PAGE_SIZE)
    total = int(summary["total"])
    if total == 0:
        return InteractionResponse("command.queue_empty")
    rows: list[dict[str, Any]] = list(summary["rows"])
    oldest = _format_age(float(rows[0]["age_seconds"]))

    # The row cap bounds how many lines we build; this bounds how long the
    # rendered message may get, which the row cap cannot -- every line carries
    # two snowflakes and a jump URL, so 25 rows can exceed Discord's limit on
    # their own. Measure the surrounding template in this locale instead of
    # guessing at it, and reserve the worst-case "N more" trailer (``more`` can
    # never exceed ``total``) so adding it later cannot push us over.
    frame = translate(
        "command.queue", ctx.locale, count=total, oldest=oldest, listing="", truncated=""
    )
    reserve = translate("command.queue_truncated", ctx.locale, more=total)
    budget = DISCORD_MESSAGE_LIMIT - len(frame) - len(reserve)

    lines: list[str] = []
    used = 0
    for row in rows:
        url = jump_url(ctx.guild_id, int(row["channel_id"]), int(row["message_id"]))
        age = _format_age(float(row["age_seconds"]))
        images = int(row.get("images", 1))
        line = (
            f"\u2022 [#{row['detection_id']}]({url}) \u2014 {row['verdict']}, "
            f"<@{row['uploader_id']}>, {age} old" + (f", {images} images" if images > 1 else "")
        )
        cost = len(line) + (1 if lines else 0)  # the "\n" join adds one each
        # Always emit the oldest row: a listing of nothing under a header that
        # says "N waiting" would read as a bug rather than as truncation.
        if lines and used + cost > budget:
            break
        lines.append(line)
        used += cost

    truncated = ""
    if total > len(lines):
        truncated = translate("command.queue_truncated", ctx.locale, more=total - len(lines))
    return InteractionResponse(
        "command.queue",
        {
            "count": total,
            "listing": "\n".join(lines),
            "truncated": truncated,
            "oldest": oldest,
        },
    )


async def _reopen_card(
    ctx: InteractionContext, deps: InteractionDeps, detection_id: int
) -> InteractionResponse:
    """Post report ``detection_id`` again as a full card with fresh buttons.

    Decided cards fold and lose their buttons, so this is how a misclick gets
    corrected: the new card covers every image of the same message, and
    pressing a different decision on it simply applies that decision. The old
    folded card stays as the record of the first call. Guild-scoped like
    every detection read, so another server's number resolves to nothing.
    """
    assert ctx.guild_id is not None
    det = await deps.get_detection(ctx.guild_id, detection_id)
    if det is None:
        return InteractionResponse("button.detection_missing", {"detection_id": detection_id})
    group = await deps.get_message_detections(ctx.guild_id, det.message_id)
    card_id = await deps.repost_review_card(ctx.guild_id, group or [det])
    if card_id is None:
        return InteractionResponse("command.queue_reopen_failed", {"detection_id": detection_id})
    await deps.audit(ctx.guild_id, ctx.user_id, "review.reopen", target=str(detection_id))
    return InteractionResponse("command.queue_reopened", {"detection_id": detection_id})


async def _cmd_help(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    return InteractionResponse("command.help")


async def _cmd_delete_server_data(
    ctx: InteractionContext, deps: InteractionDeps
) -> InteractionResponse:
    # The destructive purge itself is gated behind the confirm button
    # (component handler); this only renders the confirmation prompt.
    return InteractionResponse("command.delete_server_confirm")


_CommandHandler = Callable[
    ["InteractionContext", "InteractionDeps"], Awaitable["InteractionResponse"]
]

_COMMAND_HANDLERS: dict[str, _CommandHandler] = {
    "scamhash": _cmd_scamhash,
    "config": _cmd_config,
    "setup": _cmd_setup,
    "stats": _cmd_stats,
    "queue": _cmd_queue,
    "global": _cmd_global,
    "help": _cmd_help,
    "delete_server_data": _cmd_delete_server_data,
    "review_message": _cmd_review_message,
    "report_message": _cmd_report_message,
    "report": _cmd_report_message,
}


def _hashes_to_guild_hash(hashes: AttachmentHashes, added_by: int) -> GuildHash:
    """Build a :class:`GuildHash` from a freshly hashed ``/scamhash add`` image.

    ``hash_id`` is derived deterministically from the perceptual hash so
    re-adding the same image is idempotent: the server's existing row is kept,
    with its original attribution (see ``DbDeps.add_guild_hash``).
    """
    return GuildHash(
        hash_id=f"{hashes.phash:016x}",
        phash=hashes.phash,
        dhash=hashes.dhash,
        whash=hashes.whash,
        ahash=hashes.ahash,
        mphash=hashes.mphash,
        mdhash=hashes.mdhash,
        mwhash=hashes.mwhash,
        mahash=hashes.mahash,
        source="local",
        added_by=added_by,
    )


async def _import_hashes(
    deps: InteractionDeps, guild_id: int, entries: list[_ImportHash], *, added_by: int
) -> int:
    added = 0
    # Hashes this server already lists count as skipped, so re-importing a
    # file (or importing one that overlaps) reports "0 added" rather than
    # failing. Duplicates within the file are skipped the same way.
    seen: set[str] = {row.hash_id for row in await deps.list_guild_hashes(guild_id)}
    for entry in entries:
        hash_id = f"{entry.phash:016x}"
        if hash_id in seen:
            continue
        seen.add(hash_id)
        await deps.add_guild_hash(
            guild_id,
            GuildHash(
                hash_id=hash_id,
                phash=entry.phash,
                dhash=entry.dhash,
                whash=entry.whash,
                ahash=0,
                source="import",
                added_by=added_by,
            ),
        )
        added += 1
    return added


# --- component (button) handlers -------------------------------------------------


def _card_note(
    action: ReviewAction,
    user_id: int,
    *,
    key: str = "card.handled",
    whitelisted: Sequence[GuildWhitelist] = (),
) -> dict[str, Any]:
    """The ``card_note_*`` kwargs marking a card as handled by ``user_id``.

    ``whitelisted`` are the whitelist entries the decision created: the card
    names their numbers, so a misclick can be undone with
    ``/scamhash unwhitelist``.
    """
    params: dict[str, Any] = {"action": BUTTON_LABELS[action], "user_id": user_id}
    if whitelisted:
        key = f"{key}_whitelisted"
        params["count"] = len(whitelisted)
        params["entries"] = _entry_refs(whitelisted)
    return {"card_note_key": key, "card_note_params": params}


async def _is_global_participant(deps: InteractionDeps, guild_id: int) -> bool:
    """Whether this server's verdicts may touch the shared scam list.

    Both halves are required, and the same pair gates voting an entry *in* and
    revoking one *out*: the server opted in (``optin_global_db``) and the bot
    owner approved it for contribution. Without the check on revocation, any
    server -- including one a scammer set up for the purpose -- could post
    their own image, report it, press False positive, and pull it off the list
    for every server at once.
    """
    config = await deps.get_config(guild_id)
    if not bool(config.get("optin_global_db", False)):
        return False
    return await deps.is_trusted_guild(guild_id)


async def _resolve_image_hashes(deps: InteractionDeps, det: DetectionFacts) -> ImageHashes | None:
    """Resolve the hash ensemble for the image behind ``det``.

    Prefers the hashes persisted at detection time; falls back to re-fetching
    the attachment (member reports never hash up front). Both the REST lookup
    and the fetch+decode happen *before* the caller's first DB write on
    purpose: SQLite holds the write lock from the first INSERT/UPDATE to
    commit, and network work inside that window starves concurrent writers
    (see :meth:`InteractionDeps.compute_attachment_hashes`).
    """
    stored = det.hashes
    if stored is not None:
        try:
            return ImageHashes(
                phash=int(stored["phash"]),
                dhash=int(stored["dhash"]),
                whash=int(stored["whash"]),
                ahash=int(stored["ahash"]),
            )
        except (KeyError, TypeError, ValueError):  # pragma: no cover - corrupt row
            _log.warning("detection_hashes_corrupt", detection_id=det.detection_id)
    url = await deps.rest_attachment_url(det.channel_id, det.message_id, det.attachment_id)
    if url is None:
        return None
    try:
        computed = await deps.compute_attachment_hashes(attachment_id=det.attachment_id, url=url)
    except AttachmentHashError:
        return None
    return ImageHashes(
        phash=computed.phash,
        dhash=computed.dhash,
        whash=computed.whash,
        ahash=computed.ahash,
        mphash=computed.mphash,
        mdhash=computed.mdhash,
        mwhash=computed.mwhash,
        mahash=computed.mahash,
    )


def _hash_ensemble_dict(hashes: ImageHashes) -> dict[str, int]:
    """The 4-hash ensemble as stored on ``Detection.hashes`` (HashSet shape)."""
    return {
        "phash": hashes.phash,
        "dhash": hashes.dhash,
        "whash": hashes.whash,
        "ahash": hashes.ahash,
    }


def _image_hashes_to_guild_hash(hashes: ImageHashes, added_by: int) -> GuildHash:
    return GuildHash(
        hash_id=f"{hashes.phash:016x}",
        phash=hashes.phash,
        dhash=hashes.dhash,
        whash=hashes.whash,
        ahash=hashes.ahash,
        mphash=hashes.mphash,
        mdhash=hashes.mdhash,
        mwhash=hashes.mwhash,
        mahash=hashes.mahash,
        source="review_confirm",
        added_by=added_by,
    )


async def _card_group(
    ctx: InteractionContext, deps: InteractionDeps, det: DetectionFacts
) -> list[DetectionFacts]:
    """Every detection the pressed card covers, lowest id first.

    Images from one message share one card, so its buttons act on all of
    them. The card is identified by the message the button lives on, and only
    trusted when the button's own detection is on it -- a forged custom id
    cannot widen the action to another card. Cards posted before migration
    0012 carry no link and resolve to the single detection, as before.
    """
    assert ctx.guild_id is not None
    if ctx.card_message_id is None:
        return [det]
    on_card = await deps.get_card_detections(ctx.guild_id, ctx.card_message_id)
    if not any(d.detection_id == det.detection_id for d in on_card):
        return [det]
    return on_card


def _distinct_images(dets: Sequence[DetectionFacts]) -> list[DetectionFacts]:
    """One detection per attachment (lowest id), for the per-image writes.

    A confirmed verdict records its own row for the same attachment, so a
    reopened card can list an image twice; hashing, whitelisting and voting
    must still happen once per image.
    """
    seen: set[tuple[int, int]] = set()
    out: list[DetectionFacts] = []
    for det in dets:
        key = (det.message_id, det.attachment_id)
        if key not in seen:
            seen.add(key)
            out.append(det)
    return out


async def handle_review_button(
    ctx: InteractionContext, parsed: ParsedCustomId, deps: InteractionDeps
) -> InteractionResponse:
    """Handle a report button after re-checking the clicker's permission.

    Each action requires the permission in :data:`REVIEW_ACTION_PERMISSIONS`;
    the check runs on *this* click's member permissions, never the message's
    original author or any cached value. The detection lookup is guild-scoped,
    so a forged ``custom_id`` carrying another guild's detection id resolves to
    nothing here.

    One card covers every flagged image of a message (see :func:`_card_group`),
    so each action applies to all of them: one delete / ban / unban per
    message or uploader, one hash / whitelist write per image, one state change
    and audit row per detection.
    """
    _require(ctx, review_action_permission(parsed.action))
    assert ctx.guild_id is not None
    action = parsed.action
    detection_id = parsed.detection_id

    primary = await deps.get_detection(ctx.guild_id, detection_id)
    if primary is None:
        return InteractionResponse("button.detection_missing", {"detection_id": detection_id})
    group = await _card_group(ctx, deps, primary)
    images = _distinct_images(group)

    if action is ReviewAction.CONFIRM_SCAM:
        # All REST/network work runs before the first DB write -- see
        # _resolve_image_hashes on why that ordering is load-bearing. Every
        # image is hashed BEFORE the message is deleted: re-hashing a member
        # report needs the attachment, which the delete takes with it.
        resolved = [(det, await _resolve_image_hashes(deps, det)) for det in images]
        for channel_id, message_id in dict.fromkeys((d.channel_id, d.message_id) for d in group):
            await deps.rest_delete_message(channel_id, message_id)
        for det, hashes in resolved:
            if hashes is None:
                continue
            stored = _image_hashes_to_guild_hash(hashes, ctx.user_id)
            await deps.add_guild_hash(ctx.guild_id, stored)
            if det.hashes is None:
                # Member reports are filed without hashes; now that a moderator
                # confirmed and we re-hashed the image, keep the result so the
                # other buttons (whitelist, submit to global) still work after
                # the original message -- just deleted above -- is gone.
                await deps.set_detection_hashes(
                    ctx.guild_id, det.detection_id, _hash_ensemble_dict(hashes)
                )
        for det in group:
            await deps.set_detection_action(ctx.guild_id, det.detection_id, "confirmed")
            await deps.audit(
                ctx.guild_id, ctx.user_id, "review.confirm_scam", target=str(det.detection_id)
            )
        hashed = [(det, hashes) for det, hashes in resolved if hashes is not None]
        # A confirmed scam must not stay exempt: lift any whitelist entry
        # that covers it (an earlier misclick), and say so on the card.
        lifted = await _lift_whitelist(
            deps,
            ctx.guild_id,
            ctx.user_id,
            [h.phash for _det, h in hashed],
            cause=f"Confirm scam on detection #{detection_id}",
        )
        for det, hashes in hashed:
            # Route the confirmation through the same verdict pipeline a live
            # detection uses. Without this, Confirm deleted the single message
            # above and stopped: no ban, and therefore none of the enforcement
            # that follows one -- Discord's cross-channel ban purge, the
            # campaign sweep, the audited action row. A moderator pressing
            # "confirm scam" clearly intends the guild's configured
            # action_policy (e.g. delete + ban) to apply, exactly as it would
            # have if the hash had matched on upload. The card id lets the
            # outcome land on this (folded) card instead of a new one.
            await deps.submit_confirmed_scam(
                ctx.guild_id,
                channel_id=det.channel_id,
                message_id=det.message_id,
                attachment_id=det.attachment_id,
                uploader_id=det.uploader_id,
                matched_hash_id=f"{hashes.phash:016x}",
                confirmed_by=ctx.user_id,
                review_card_id=ctx.card_message_id,
                whitelist_removed=len(lifted),
            )
        key = "button.confirmed_scam" if hashed else "button.confirmed_no_hash"
        # Confirm doubles as the global promotion vote — but only from servers
        # the owner approved AND that opted in. Everyone else's confirm stays
        # purely local; the vote can also be refused (rate limit/reputation)
        # without affecting the local confirm, which already happened above.
        if hashed and await _is_global_participant(deps, ctx.guild_id):
            votes: list[str] = []
            for _det, hashes in hashed:
                vote = await deps.global_vote(
                    hash_id=f"{hashes.phash:016x}",
                    phash=hashes.phash,
                    dhash=hashes.dhash,
                    whash=hashes.whash,
                    voter_user_id=ctx.user_id,
                    voter_guild_id=ctx.guild_id,
                )
                if vote is not None:
                    votes.append(vote)
                    await deps.audit(
                        ctx.guild_id,
                        ctx.user_id,
                        "global.vote",
                        target=f"{hashes.phash:016x}",
                    )
            if "promoted" in votes:
                key = "button.confirmed_scam_promoted"
            elif votes:
                key = "button.confirmed_scam_voted"
        return InteractionResponse(
            key, {"detection_id": detection_id}, **_card_note(action, ctx.user_id)
        )

    if action is ReviewAction.FALSE_POSITIVE:
        resolved = [(det, await _resolve_image_hashes(deps, det)) for det in images]
        # If enforcement already banned the uploader, a false positive must
        # actually free them -- best-effort, before the first DB write. But an
        # unban is a Ban Members power: False positive is on Manage Messages so
        # every moderator can correct a wrong call, and without this check it
        # would be a second, ungated route around the Unban button. A mod
        # without Ban Members still marks the call; the ban is left for someone
        # who holds it, and the card says so to everyone watching.
        can_unban = has_permission(ctx.member_permissions, Permission.BAN_MEMBERS)
        if can_unban:
            for uploader_id in dict.fromkeys(d.uploader_id for d in group):
                await deps.rest_unban(
                    ctx.guild_id,
                    uploader_id,
                    reason=reasons.false_positive_reason(detection_id),
                )
        created: list[GuildWhitelist] = []
        for det, hashes in resolved:
            if hashes is None:
                continue
            created.append(
                await deps.add_whitelist(
                    ctx.guild_id,
                    GuildWhitelist(
                        phash=hashes.phash,
                        dhash=hashes.dhash,
                        whash=hashes.whash,
                        reason=f"false positive: detection #{det.detection_id}",
                        added_by=ctx.user_id,
                    ),
                )
            )
        for det in group:
            await deps.reverse_detection_action(ctx.guild_id, det.detection_id)
            await deps.audit(
                ctx.guild_id, ctx.user_id, "review.false_positive", target=str(det.detection_id)
            )
        disputed = [h for _det, h in resolved if h is not None]
        key = "button.marked_false_positive" if disputed else "button.marked_false_positive_no_hash"
        # A false positive from a participating server kills the global entry:
        # revoke immediately and dock the submitter's reputation. One bad
        # community poisoning the shared set costs it credibility; a legitimate
        # mistake self-corrects. Anywhere else the verdict stays local -- the
        # whitelist above still keeps this server from flagging the image.
        if disputed and await _is_global_participant(deps, ctx.guild_id):
            for entry in disputed:
                if await deps.global_dispute(f"{entry.phash:016x}"):
                    await deps.audit(
                        ctx.guild_id,
                        ctx.user_id,
                        "global.dispute",
                        target=f"{entry.phash:016x}",
                    )
                    key = "button.marked_false_positive_global_revoked"
        note_key = "card.handled" if can_unban else "card.handled_ban_kept"
        return InteractionResponse(
            key,
            {"detection_id": detection_id},
            **_card_note(action, ctx.user_id, key=note_key, whitelisted=created),
        )

    if action is ReviewAction.DISMISS:
        # The queue's only no-op exit. Confirm writes the image into the
        # guild blocklist and False positive writes it into the whitelist --
        # both wrong for a report that was simply mistaken, because a
        # whitelisted image is permanently exempt from detection and a
        # member's bad report must never buy that exemption. Dismiss records
        # that a moderator looked and chose to do nothing: no hash write, no
        # whitelist write, no REST call, nothing restored (a member report
        # never deleted anything). It exists so /queue can actually drain --
        # without a terminal state these rows sit at action_taken='none'
        # forever. The reporter is deliberately not told, so mass-reporting
        # cannot be used to probe what does and does not get through.
        for det in group:
            await deps.set_detection_action(ctx.guild_id, det.detection_id, "dismissed")
            await deps.audit(
                ctx.guild_id, ctx.user_id, "review.dismiss", target=str(det.detection_id)
            )
        return InteractionResponse(
            "button.dismissed", {"detection_id": detection_id}, **_card_note(action, ctx.user_id)
        )

    if action is ReviewAction.BAN_UPLOADER:
        config = await deps.get_config(ctx.guild_id)
        purge_hours = min(int(config.get("ban_purge_hours", 24)), 168)  # Discord caps at 7d
        banned = False
        for uploader_id in dict.fromkeys(d.uploader_id for d in group):
            banned = (
                await deps.rest_ban(
                    ctx.guild_id,
                    uploader_id,
                    reason=reasons.confirmed_reason(detection_id),
                    purge_seconds=purge_hours * 3600,
                )
                or banned
            )
        if not banned:
            return InteractionResponse("button.action_failed")
        for det in group:
            await deps.set_detection_action(ctx.guild_id, det.detection_id, "banned")
            await deps.audit(
                ctx.guild_id, ctx.user_id, "review.ban_uploader", target=str(det.detection_id)
            )
        return InteractionResponse("button.uploader_banned", **_card_note(action, ctx.user_id))

    if action is ReviewAction.UNBAN:
        unbanned = False
        for uploader_id in dict.fromkeys(d.uploader_id for d in group):
            unbanned = (
                await deps.rest_unban(
                    ctx.guild_id,
                    uploader_id,
                    reason=reasons.manual_unban_reason(detection_id),
                )
                or unbanned
            )
        if not unbanned:
            return InteractionResponse("button.action_failed")
        await deps.audit(ctx.guild_id, ctx.user_id, "review.unban", target=str(detection_id))
        return InteractionResponse("button.uploader_unbanned", **_card_note(action, ctx.user_id))

    if action is ReviewAction.WHITELIST_IMAGE:
        resolved = [(det, await _resolve_image_hashes(deps, det)) for det in images]
        if all(hashes is None for _det, hashes in resolved):
            return InteractionResponse("button.no_image")
        created = []
        for det, hashes in resolved:
            if hashes is None:
                continue
            created.append(
                await deps.add_whitelist(
                    ctx.guild_id,
                    GuildWhitelist(
                        phash=hashes.phash,
                        dhash=hashes.dhash,
                        whash=hashes.whash,
                        reason=f"review: detection #{det.detection_id}",
                        added_by=ctx.user_id,
                    ),
                )
            )
            await deps.audit(
                ctx.guild_id, ctx.user_id, "review.whitelist_image", target=str(det.detection_id)
            )
        return InteractionResponse(
            "button.image_whitelisted", **_card_note(action, ctx.user_id, whitelisted=created)
        )

    if action is ReviewAction.SUBMIT_GLOBAL:
        # Legacy button on cards rendered before global sharing became
        # automatic. Confirm scam now casts the global vote itself (on
        # approved, opted-in servers), so this button only explains itself.
        return InteractionResponse("button.submit_global_removed")
    raise InteractionRejected(CommandError.UNKNOWN_FIELD)  # pragma: no cover


async def handle_component(
    ctx: InteractionContext, action: ComponentAction, ref_id: int, deps: InteractionDeps
) -> InteractionResponse:
    """Handle a non-report component (appeal lifecycle, safe-mode, purge confirm)."""
    if action is ComponentAction.APPEAL_OPEN:
        if ctx.guild_id is None:
            raise InteractionRejected(CommandError.GUILD_ONLY)
        # The detection id rides in the (client-echoed, forgeable) custom id, so
        # never trust it: only the user the detection was filed against may appeal
        # it, and only within the detection's own guild, so ownership is
        # re-verified server-side here.
        if not await deps.detection_belongs_to(ctx.guild_id, ref_id, ctx.user_id):
            return InteractionResponse("command.appeal_none")
        if not await deps.appeal_cooldown_ok(ctx.user_id):
            return InteractionResponse("dm.appeal_cooldown")
        await deps.open_appeal(ctx.guild_id, ref_id, ctx.user_id)
        await deps.audit(ctx.guild_id, ctx.user_id, "appeal.open", target=str(ref_id))
        return InteractionResponse("dm.appeal_submitted")

    # The remaining controls are all moderator/admin state changes.
    if action in (ComponentAction.APPEAL_APPROVE, ComponentAction.APPEAL_DENY):
        _require(ctx, Permission.MANAGE_GUILD)
        assert ctx.guild_id is not None
        approved = action is ComponentAction.APPEAL_APPROVE
        await deps.resolve_appeal(ctx.guild_id, ref_id, approved=approved)
        if approved:
            appeal = await deps.get_appeal(ctx.guild_id, ref_id)
            if appeal is not None:
                detection_id = int(appeal["detection_id"])
                # An approved appeal must actually lift the enforcement, not
                # just mark the row: unban is best-effort (a no-op failure if
                # the user was never banned).
                await deps.rest_unban(
                    ctx.guild_id,
                    int(appeal["user_id"]),
                    reason=reasons.appeal_approved_reason(detection_id),
                )
                await deps.reverse_detection_action(ctx.guild_id, detection_id)
            await deps.audit(ctx.guild_id, ctx.user_id, "appeal.approve", target=str(ref_id))
            return InteractionResponse("button.appeal_approved")
        await deps.audit(ctx.guild_id, ctx.user_id, "appeal.deny", target=str(ref_id))
        return InteractionResponse("button.appeal_denied")

    if action is ComponentAction.SAFE_MODE_RESUME:
        _require(ctx, Permission.MANAGE_GUILD)
        assert ctx.guild_id is not None
        await deps.disable_safe_mode(ctx.guild_id)
        await deps.audit(ctx.guild_id, ctx.user_id, "safe_mode.resume")
        return InteractionResponse("button.safe_mode_resumed")

    if action is ComponentAction.DELETE_SERVER_CONFIRM:
        _require(ctx, Permission.ADMINISTRATOR)
        assert ctx.guild_id is not None
        # A full GDPR purge erases the audit log too, so recording a row here
        # would be immediately deleted; the purge is the audited event itself.
        await deps.purge_guild(ctx.guild_id)
        return InteractionResponse("command.delete_server_ok")

    raise InteractionRejected(CommandError.UNKNOWN_FIELD)  # pragma: no cover
