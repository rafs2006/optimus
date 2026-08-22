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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from optimus.core.logging import get_logger
from optimus.db.models import GuildHash, GuildWhitelist
from optimus.i18n import translate
from optimus.services.interactions.attachment_hash import (
    AttachmentHashError,
    AttachmentHashes,
)
from optimus.services.interactions.commands import required_permission
from optimus.services.interactions.logic import (
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
from optimus.services.moderation.review import BUTTON_LABELS, ParsedCustomId, ReviewAction

_log = get_logger(__name__)

#: Keep ``/scamhash list`` safely below Discord's 2,000-character message limit
#: even if every rendered line maxes out (64-char hash id, 32-char source, and
#: a full-width ``by <@user>`` mention ≈ 130 chars per line).
_HASH_LIST_PREVIEW_LIMIT = 12


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


class InteractionDeps(Protocol):
    """Side-effecting collaborators a handler needs, all per-request scoped."""

    async def add_guild_hash(self, guild_id: int, gh: GuildHash) -> GuildHash: ...
    async def remove_guild_hash(self, guild_id: int, hash_id: str) -> int: ...
    async def list_guild_hashes(self, guild_id: int) -> list[GuildHash]: ...
    async def add_whitelist(self, guild_id: int, entry: GuildWhitelist) -> GuildWhitelist: ...
    async def get_config(self, guild_id: int) -> dict[str, Any]: ...
    async def set_config_field(self, guild_id: int, field: str, value: Any) -> None: ...
    async def stats_summary(self, guild_id: int) -> dict[str, Any]: ...
    async def opt_out_user(self, user_id: int) -> int: ...
    async def purge_guild(self, guild_id: int) -> int: ...
    async def recent_detection_for(self, guild_id: int, user_id: int) -> int | None: ...
    async def detection_belongs_to(
        self, guild_id: int, detection_id: int, user_id: int
    ) -> bool: ...
    async def open_appeal(self, guild_id: int, detection_id: int, user_id: int) -> int: ...
    async def get_appeal(self, guild_id: int, appeal_id: int) -> dict[str, Any] | None: ...
    async def resolve_appeal(self, guild_id: int, appeal_id: int, *, approved: bool) -> None: ...
    async def reverse_detection_action(self, guild_id: int, detection_id: int) -> None: ...
    async def get_detection(self, guild_id: int, detection_id: int) -> DetectionFacts | None: ...
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
    ) -> int | None:
        """Create the private review channel; ``None`` when REST is unavailable/refused."""
        ...

    async def disable_safe_mode(self, guild_id: int) -> None: ...
    async def local_hash(self, guild_id: int, hash_id: str) -> GuildHash | None: ...
    async def enforcement_blocked(
        self, guild_id: int, channel_id: int, *, action: str, locale: str
    ) -> str | None:
        """Why enforcement in this channel would be refused, or ``None``."""
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
    _require(ctx, required_permission(ctx.command))
    handler = _COMMAND_HANDLERS.get(ctx.command)
    if handler is None:  # pragma: no cover - registration guarantees coverage
        raise InteractionRejected(CommandError.UNKNOWN_FIELD)
    return await handler(ctx, deps)


async def _cmd_scamhash(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    assert ctx.guild_id is not None  # guaranteed by _require (MANAGE_GUILD => guild-only)
    sub = ctx.subcommand
    if sub == "add":
        if not await deps.hash_rate_ok(ctx.user_id):
            raise InteractionRejected(CommandError.RATE_LIMITED)
        attachment_id, url = ctx.options.get("attachment_id"), ctx.options.get("url")
        if attachment_id is None or url is None:
            # The glue layer found no usable image on the interaction (wrong
            # file type, or Discord sent no resolved attachment).
            return InteractionResponse("command.add_not_image")
        try:
            hashes = await deps.compute_attachment_hashes(
                attachment_id=int(attachment_id), url=str(url)
            )
        except AttachmentHashError as exc:
            return InteractionResponse("command.add_fetch_failed", {"reason": str(exc)})
        stored = await deps.add_guild_hash(ctx.guild_id, _hashes_to_guild_hash(hashes, ctx.user_id))
        await deps.audit(ctx.guild_id, ctx.user_id, "scamhash.add", target=stored.hash_id)
        return InteractionResponse("command.hash_added", {"hash_id": stored.hash_id})
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
        shown = sorted(rows, key=lambda r: r.hash_id)[:_HASH_LIST_PREVIEW_LIMIT]
        params: dict[str, Any] = {
            "count": len(rows),
            "hashes": "\n".join(_render_hash_entry(r) for r in shown),
        }
        if len(rows) <= _HASH_LIST_PREVIEW_LIMIT:
            return InteractionResponse("command.hash_list_header", params)
        params["remaining"] = len(rows) - len(shown)
        return InteractionResponse("command.hash_list_truncated", params)
    if sub == "import":
        entries = validate_import(str(ctx.options["file"]))
        added = await _import_hashes(deps, ctx.guild_id, entries, added_by=ctx.user_id)
        await deps.audit(ctx.guild_id, ctx.user_id, "scamhash.import", target=str(added))
        return InteractionResponse(
            "command.import_ok", {"added": added, "skipped": len(entries) - added}
        )
    if sub == "export":
        rows = await deps.list_guild_hashes(ctx.guild_id)
        if not rows:
            return InteractionResponse("command.export_empty")
        body = build_export(
            [_ImportHash(phash=r.phash, dhash=r.dhash, whash=r.whash) for r in rows]
        )
        return InteractionResponse("command.export_ok", {"count": len(rows)}, attachment=body)
    if sub == "review":
        return await _review_message(ctx, deps)
    raise InteractionRejected(CommandError.UNKNOWN_FIELD)  # pragma: no cover


def _render_hash_entry(row: GuildHash) -> str:
    """One display line per hash: id, source, and who added it (when known)."""
    added_by = f" by <@{row.added_by}>" if row.added_by is not None else ""
    return f"\u2022 `{row.hash_id}` \u2014 {row.source}{added_by}"


async def _cmd_report_message(
    ctx: InteractionContext, deps: InteractionDeps
) -> InteractionResponse:
    """Entry point for the member-facing "Report scam to mods" context menu.

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
    await deps.submit_user_report(
        ctx.guild_id,
        channel_id=int(ctx.options["channel_id"]),
        message_id=message_id,
        attachment_id=attachments[0][0],
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
    ``/scamhash review`` (``MANAGE_GUILD``); the glue layer has already
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
    assert ctx.guild_id is not None  # guaranteed by _require (MANAGE_GUILD => guild-only)
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
        return InteractionResponse(
            "command.config_view_header", {"summary": _render_config_summary(current, locale)}
        )
    change = validate_config_set(str(ctx.options["field"]), str(ctx.options["value"]))
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
    "optin_evidence_storage",
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
        if config_field == "review_channel":
            rendered = f"<#{value}>" if value is not None else "not set"
        else:
            rendered = str(value)
        lines.append(f"**{config_field}**: `{rendered}`")
        lines.append(f"-# {translate(f'config_help.{config_field}', locale)}")
    return "\n".join(lines)


def _render_config_value(field: str, value: Any) -> str:
    """Render a validated config value for the ``config_set_ok`` confirmation.

    ``review_channel`` stores a raw channel id (or ``None`` when cleared); show
    it as a real channel mention (or "none") instead of a bare integer/"None".
    Every other field renders as-is.
    """
    if field == "review_channel":
        return f"<#{value}>" if value is not None else "none"
    return str(value)


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
    if created is None:
        return InteractionResponse("command.setup_failed")
    await deps.set_config_field(ctx.guild_id, "review_channel", created)
    await deps.audit(ctx.guild_id, ctx.user_id, "setup.review_channel", target=str(created))
    key = "command.setup_created" if mod_role is not None else "command.setup_created_no_role"
    return InteractionResponse(key, {"channel_id": created})


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
        },
    )


async def _cmd_global(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    # Owner-only: Discord has no "application owner" permission flag, so the
    # gate is enforced here against the application's owner/team ids. An empty
    # set (lookup failed) refuses — fail closed on the trust-granting command.
    if ctx.user_id not in await deps.rest_owner_ids():
        return InteractionResponse("command.owner_only")
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
    if ctx.subcommand == "revoke_server":
        removed = await deps.untrust_guild(server_id)
        if ctx.guild_id is not None:
            await deps.audit(ctx.guild_id, ctx.user_id, "global.revoke_server", target=raw)
        key = "command.global_server_revoked" if removed else "command.global_server_missing"
        return InteractionResponse(key, {"server_id": raw})
    raise InteractionRejected(CommandError.UNKNOWN_FIELD)  # pragma: no cover


async def _cmd_help(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    return InteractionResponse("command.help")


async def _cmd_delete_server_data(
    ctx: InteractionContext, deps: InteractionDeps
) -> InteractionResponse:
    # The destructive purge itself is gated behind the confirm button
    # (component handler); this only renders the confirmation prompt.
    return InteractionResponse("command.delete_server_confirm")


async def _cmd_forget_me(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    await deps.opt_out_user(ctx.user_id)
    if ctx.guild_id is not None:
        await deps.audit(ctx.guild_id, ctx.user_id, "forget_me", target=str(ctx.user_id))
    return InteractionResponse("command.forget_me_ok")


async def _cmd_appeal(ctx: InteractionContext, deps: InteractionDeps) -> InteractionResponse:
    assert ctx.guild_id is not None
    if not await deps.appeal_cooldown_ok(ctx.user_id):
        return InteractionResponse("dm.appeal_cooldown")
    detection_id = await deps.recent_detection_for(ctx.guild_id, ctx.user_id)
    if detection_id is None:
        return InteractionResponse("command.appeal_none")
    await deps.open_appeal(ctx.guild_id, detection_id, ctx.user_id)
    await deps.audit(ctx.guild_id, ctx.user_id, "appeal.open", target=str(detection_id))
    return InteractionResponse("command.appeal_opened")


_CommandHandler = Callable[
    ["InteractionContext", "InteractionDeps"], Awaitable["InteractionResponse"]
]

_COMMAND_HANDLERS: dict[str, _CommandHandler] = {
    "scamhash": _cmd_scamhash,
    "config": _cmd_config,
    "setup": _cmd_setup,
    "stats": _cmd_stats,
    "global": _cmd_global,
    "help": _cmd_help,
    "delete_server_data": _cmd_delete_server_data,
    "forget_me": _cmd_forget_me,
    "appeal": _cmd_appeal,
    "review_message": _cmd_review_message,
    "report_message": _cmd_report_message,
}


def _hashes_to_guild_hash(hashes: AttachmentHashes, added_by: int) -> GuildHash:
    """Build a :class:`GuildHash` from a freshly hashed ``/scamhash add`` image.

    ``hash_id`` is derived deterministically from the perceptual hash so
    re-adding the same image is idempotent (the upsert replaces the row).
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
    seen: set[str] = set()
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


def _card_note(action: ReviewAction, user_id: int) -> dict[str, Any]:
    """The ``card_note_*`` kwargs marking a card as handled by ``user_id``."""
    return {
        "card_note_key": "card.handled",
        "card_note_params": {"action": BUTTON_LABELS[action], "user_id": user_id},
    }


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


async def handle_review_button(
    ctx: InteractionContext, parsed: ParsedCustomId, deps: InteractionDeps
) -> InteractionResponse:
    """Handle a report button after re-checking the clicker's permission.

    Every report action is a state change requiring ``MANAGE_GUILD``; the check
    runs on *this* click's member permissions, never the message's original
    author or any cached value. The detection lookup is guild-scoped, so a
    forged ``custom_id`` carrying another guild's detection id resolves to
    nothing here.
    """
    _require(ctx, Permission.MANAGE_GUILD)
    assert ctx.guild_id is not None
    action = parsed.action
    detection_id = parsed.detection_id

    det = await deps.get_detection(ctx.guild_id, detection_id)
    if det is None:
        return InteractionResponse("button.detection_missing", {"detection_id": detection_id})

    if action is ReviewAction.CONFIRM_SCAM:
        # All REST/network work runs before the first DB write -- see
        # _resolve_image_hashes on why that ordering is load-bearing.
        hashes = await _resolve_image_hashes(deps, det)
        await deps.rest_delete_message(det.channel_id, det.message_id)
        if hashes is not None:
            await deps.add_guild_hash(
                ctx.guild_id, _image_hashes_to_guild_hash(hashes, ctx.user_id)
            )
            if det.hashes is None:
                # Member reports are filed without hashes; now that a moderator
                # confirmed and we re-hashed the image, keep the result so the
                # other buttons (whitelist, submit to global) still work after
                # the original message -- just deleted above -- is gone.
                await deps.set_detection_hashes(
                    ctx.guild_id, detection_id, _hash_ensemble_dict(hashes)
                )
        await deps.set_detection_action(ctx.guild_id, detection_id, "confirmed")
        await deps.audit(ctx.guild_id, ctx.user_id, "review.confirm_scam", target=str(detection_id))
        if hashes is not None:
            # Route the confirmation through the same verdict pipeline a live
            # detection uses. Without this, Confirm deleted the single message
            # above and stopped: no ban, and therefore none of the enforcement
            # that follows one -- Discord's cross-channel ban purge, the
            # campaign sweep, the audited action row. A moderator pressing
            # "confirm scam" clearly intends the guild's configured
            # action_policy (e.g. delete + ban) to apply, exactly as it would
            # have if the hash had matched on upload.
            await deps.submit_confirmed_scam(
                ctx.guild_id,
                channel_id=det.channel_id,
                message_id=det.message_id,
                attachment_id=det.attachment_id,
                uploader_id=det.uploader_id,
                matched_hash_id=f"{hashes.phash:016x}",
            )
        key = "button.confirmed_scam" if hashes is not None else "button.confirmed_no_hash"
        # Confirm doubles as the global promotion vote — but only from servers
        # the owner approved AND that opted in. Everyone else's confirm stays
        # purely local; the vote can also be refused (rate limit/reputation)
        # without affecting the local confirm, which already happened above.
        if hashes is not None:
            config = await deps.get_config(ctx.guild_id)
            if bool(config.get("optin_global_db", False)) and await deps.is_trusted_guild(
                ctx.guild_id
            ):
                vote = await deps.global_vote(
                    hash_id=f"{hashes.phash:016x}",
                    phash=hashes.phash,
                    dhash=hashes.dhash,
                    whash=hashes.whash,
                    voter_user_id=ctx.user_id,
                    voter_guild_id=ctx.guild_id,
                )
                if vote is not None:
                    await deps.audit(
                        ctx.guild_id,
                        ctx.user_id,
                        "global.vote",
                        target=f"{hashes.phash:016x}",
                    )
                    key = (
                        "button.confirmed_scam_promoted"
                        if vote == "promoted"
                        else "button.confirmed_scam_voted"
                    )
        return InteractionResponse(
            key, {"detection_id": detection_id}, **_card_note(action, ctx.user_id)
        )

    if action is ReviewAction.FALSE_POSITIVE:
        hashes = await _resolve_image_hashes(deps, det)
        # If enforcement already banned the uploader, a false positive must
        # actually free them -- best-effort, before the first DB write.
        await deps.rest_unban(
            ctx.guild_id,
            det.uploader_id,
            reason=f"Optimus: detection #{detection_id} marked false positive",
        )
        if hashes is not None:
            await deps.add_whitelist(
                ctx.guild_id,
                GuildWhitelist(
                    phash=hashes.phash,
                    dhash=hashes.dhash,
                    whash=hashes.whash,
                    reason=f"false positive: detection #{detection_id}",
                    added_by=ctx.user_id,
                ),
            )
        await deps.reverse_detection_action(ctx.guild_id, detection_id)
        await deps.audit(
            ctx.guild_id, ctx.user_id, "review.false_positive", target=str(detection_id)
        )
        key = (
            "button.marked_false_positive"
            if hashes is not None
            else "button.marked_false_positive_no_hash"
        )
        # A false positive anywhere kills the global entry: revoke immediately
        # and dock the submitter's reputation. One bad community poisoning the
        # shared set costs it credibility; a legitimate mistake self-corrects.
        if hashes is not None and await deps.global_dispute(f"{hashes.phash:016x}"):
            await deps.audit(
                ctx.guild_id, ctx.user_id, "global.dispute", target=f"{hashes.phash:016x}"
            )
            key = "button.marked_false_positive_global_revoked"
        return InteractionResponse(
            key, {"detection_id": detection_id}, **_card_note(action, ctx.user_id)
        )

    if action is ReviewAction.BAN_UPLOADER:
        config = await deps.get_config(ctx.guild_id)
        purge_hours = min(int(config.get("ban_purge_hours", 24)), 168)  # Discord caps at 7d
        banned = await deps.rest_ban(
            ctx.guild_id,
            det.uploader_id,
            reason=f"Optimus: scam image (detection #{detection_id})",
            purge_seconds=purge_hours * 3600,
        )
        if not banned:
            return InteractionResponse("button.action_failed")
        await deps.set_detection_action(ctx.guild_id, detection_id, "banned")
        await deps.audit(ctx.guild_id, ctx.user_id, "review.ban_uploader", target=str(detection_id))
        return InteractionResponse(
            "button.uploader_banned",
            card_note_key="card.handled",
            card_note_params={"action": BUTTON_LABELS[action], "user_id": ctx.user_id},
        )

    if action is ReviewAction.UNBAN:
        unbanned = await deps.rest_unban(
            ctx.guild_id,
            det.uploader_id,
            reason=f"Optimus: unbanned by moderator (detection #{detection_id})",
        )
        if not unbanned:
            return InteractionResponse("button.action_failed")
        await deps.audit(ctx.guild_id, ctx.user_id, "review.unban", target=str(detection_id))
        return InteractionResponse("button.uploader_unbanned", **_card_note(action, ctx.user_id))

    if action is ReviewAction.WHITELIST_IMAGE:
        hashes = await _resolve_image_hashes(deps, det)
        if hashes is None:
            return InteractionResponse("button.no_image")
        await deps.add_whitelist(
            ctx.guild_id,
            GuildWhitelist(
                phash=hashes.phash,
                dhash=hashes.dhash,
                whash=hashes.whash,
                reason=f"review: detection #{detection_id}",
                added_by=ctx.user_id,
            ),
        )
        await deps.audit(
            ctx.guild_id, ctx.user_id, "review.whitelist_image", target=str(detection_id)
        )
        return InteractionResponse("button.image_whitelisted", **_card_note(action, ctx.user_id))

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
        # it, and only within the detection's own guild. The /appeal command path
        # derives this server-side; here we re-verify ownership explicitly.
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
                    reason=f"Optimus: appeal approved (detection #{detection_id})",
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
