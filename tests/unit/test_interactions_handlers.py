"""Interaction handlers: server-side auth, audit, button auth, appeal lifecycle."""

from __future__ import annotations

from typing import Any

import pytest

from optimus.db.models import GuildHash, GuildWhitelist
from optimus.services.interactions.attachment_hash import (
    AttachmentHashError,
    AttachmentHashes,
)
from optimus.services.interactions.handlers import (
    _CONFIG_VIEW_ORDER,
    _HASH_LIST_PREVIEW_LIMIT,
    DetectionFacts,
    InteractionContext,
    handle_command,
    handle_component,
    handle_review_button,
)
from optimus.services.interactions.logic import (
    CommandError,
    ComponentAction,
    InteractionRejected,
    Permission,
)
from optimus.services.interactions.service import render
from optimus.services.moderation.permissions import (
    MANAGE_MESSAGES,
    VIEW_CHANNEL,
    build_access_report,
)
from optimus.services.moderation.reasons import confirmed_reason
from optimus.services.moderation.review import ParsedCustomId, ReviewAction

ADMIN = int(Permission.ADMINISTRATOR)
MANAGE = int(Permission.MANAGE_GUILD)
NONE = 0


class FakeDeps:
    """An in-memory :class:`InteractionDeps` that records side effects."""

    def __init__(self, **flags: Any) -> None:
        self.audits: list[tuple[int, int, str, str | None]] = []
        self.hashes: dict[str, GuildHash] = {}
        self.appeals: dict[int, dict[str, Any]] = {}
        self.reversed: list[int] = []
        self.purged: list[int] = []
        self.safe_mode_disabled: list[int] = []
        self.config_set: list[tuple[str, Any]] = []
        self.resolved: list[tuple[int, bool]] = []
        self._hash_rate_ok = flags.get("hash_rate_ok", True)
        self._report_rate_ok = flags.get("report_rate_ok", True)
        self.user_reports: list[dict[str, Any]] = []
        self._appeal_ok = flags.get("appeal_ok", True)
        self._owned_detections: set[int] = set(flags.get("owned_detections", {77}))
        self._next_appeal_id = 1
        # -- global trust lane -------------------------------------------------
        #: guild ids approved (allowlisted) for global contribution.
        self.trusted_guilds: set[int] = set(flags.get("trusted_guilds", set()))
        #: application owner ids returned by rest_owner_ids.
        self.owner_ids: set[int] = set(flags.get("owner_ids", {1000}))
        #: what global_vote returns: "candidate", "promoted", or None (refused).
        self._vote_result = flags.get("vote_result", "candidate")
        #: hash_ids with a live global entry — global_dispute returns True.
        self._global_hashes: set[str] = set(flags.get("global_hashes", set()))
        self.global_votes: list[dict[str, Any]] = []
        self.global_disputes: list[str] = []
        #: attachment_id -> exception to raise, or a hash_id string to return.
        self._attachment_outcomes: dict[int, Any] = flags.get("attachment_outcomes", {})
        self.confirmed_scams: list[dict[str, Any]] = []
        #: Reason enforcement_blocked returns; None means "nothing in the way".
        self._enforcement_blocked: str | None = flags.get("enforcement_blocked")
        self.blocked_checks: list[tuple[int, int, str]] = []
        #: What /config permissions sees; ``None`` models "cannot check".
        self._access_report = flags.get("access_report")
        #: Extra get_config fields (e.g. action_policy/safe_mode) so reviewmsg
        #: outcome-reporting tests can drive each policy branch.
        self.config: dict[str, Any] = {"locale": "en", **flags.get("config", {})}
        # -- review-button surface -------------------------------------------
        #: Explicit rows; when absent, get_detection fabricates one per id
        #: (with stored hashes) unless ``detection_missing`` is set.
        self.detections: dict[int, DetectionFacts] = dict(flags.get("detections", {}))
        self._detection_missing = flags.get("detection_missing", False)
        #: hashes for fabricated detections; pass ``stored_hashes=None`` to
        #: model a member report filed without hashes.
        self._stored_hashes = flags.get(
            "stored_hashes", {"phash": 0xABC, "dhash": 2, "whash": 3, "ahash": 4}
        )
        self._attachment_url = flags.get("attachment_url", "https://cdn/att.png")
        self._rest_ban_ok = flags.get("rest_ban_ok", True)
        self._rest_unban_ok = flags.get("rest_unban_ok", True)
        #: id the fake provisioner returns; ``None`` models a REST refusal
        #: (bot missing Manage Channels).
        self._created_channel_id = flags.get("created_channel_id", 555)
        self.created_channels: list[dict[str, Any]] = []
        self.deleted_messages: list[tuple[int, int]] = []
        self.bans: list[dict[str, Any]] = []
        self.unbans: list[tuple[int, int]] = []
        self.detection_actions: list[tuple[int, str]] = []
        self.backfilled_hashes: list[tuple[int, dict[str, int]]] = []
        self.whitelisted: list[GuildWhitelist] = []

    async def add_guild_hash(self, guild_id: int, gh: GuildHash) -> GuildHash:
        self.hashes[gh.hash_id] = gh
        return gh

    async def remove_guild_hash(self, guild_id: int, hash_id: str) -> int:
        return 1 if self.hashes.pop(hash_id, None) is not None else 0

    async def list_guild_hashes(self, guild_id: int) -> list[GuildHash]:
        return list(self.hashes.values())

    async def add_whitelist(self, guild_id: int, entry: GuildWhitelist) -> GuildWhitelist:
        self.whitelisted.append(entry)
        return entry

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        return dict(self.config)

    async def set_config_field(self, guild_id: int, field: str, value: Any) -> None:
        self.config_set.append((field, value))

    async def stats_summary(self, guild_id: int) -> dict[str, Any]:
        return {"detections": 3, "hours": 24, "boots": 5, "first_boot": "2026-08-01"}

    async def purge_guild(self, guild_id: int) -> int:
        self.purged.append(guild_id)
        return 7

    async def detection_belongs_to(self, guild_id: int, detection_id: int, user_id: int) -> bool:
        return detection_id in self._owned_detections

    async def open_appeal(self, guild_id: int, detection_id: int, user_id: int) -> int:
        appeal_id = self._next_appeal_id
        self._next_appeal_id += 1
        self.appeals[appeal_id] = {"detection_id": detection_id, "user_id": user_id}
        return appeal_id

    async def get_appeal(self, guild_id: int, appeal_id: int) -> dict[str, Any] | None:
        return self.appeals.get(appeal_id)

    async def resolve_appeal(self, guild_id: int, appeal_id: int, *, approved: bool) -> None:
        self.resolved.append((appeal_id, approved))

    async def reverse_detection_action(self, guild_id: int, detection_id: int) -> None:
        self.reversed.append(detection_id)

    async def disable_safe_mode(self, guild_id: int) -> None:
        self.safe_mode_disabled.append(guild_id)

    async def local_hash(self, guild_id: int, hash_id: str) -> GuildHash | None:
        return self.hashes.get(hash_id)

    async def enforcement_blocked(
        self, guild_id: int, channel_id: int, *, action: str, locale: str
    ) -> str | None:
        self.blocked_checks.append((guild_id, channel_id, action))
        return self._enforcement_blocked

    async def has_pending_scan(self, guild_id: int) -> bool:
        return bool(getattr(self, "pending_scan", False))

    async def access_report(self, guild_id: int) -> Any:
        return self._access_report

    async def hash_rate_ok(self, user_id: int) -> bool:
        return self._hash_rate_ok

    async def report_rate_ok(self, user_id: int) -> bool:
        return self._report_rate_ok

    async def appeal_cooldown_ok(self, user_id: int) -> bool:
        return self._appeal_ok

    async def audit(
        self, guild_id: int, actor_id: int, action: str, *, target: str | None = None
    ) -> None:
        self.audits.append((guild_id, actor_id, action, target))

    async def is_trusted_guild(self, guild_id: int) -> bool:
        return guild_id in self.trusted_guilds

    async def trust_guild(self, guild_id: int, *, added_by: int) -> bool:
        if guild_id in self.trusted_guilds:
            return False
        self.trusted_guilds.add(guild_id)
        return True

    async def untrust_guild(self, guild_id: int) -> bool:
        if guild_id not in self.trusted_guilds:
            return False
        self.trusted_guilds.discard(guild_id)
        return True

    async def list_trusted_guilds(self) -> list[int]:
        return sorted(self.trusted_guilds)

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
        self.global_votes.append(
            {
                "hash_id": hash_id,
                "phash": phash,
                "dhash": dhash,
                "whash": whash,
                "voter_user_id": voter_user_id,
                "voter_guild_id": voter_guild_id,
            }
        )
        return self._vote_result

    async def global_dispute(self, hash_id: str) -> bool:
        self.global_disputes.append(hash_id)
        return hash_id in self._global_hashes

    async def rest_owner_ids(self) -> set[int]:
        return set(self.owner_ids)

    async def compute_attachment_hashes(self, *, attachment_id: int, url: str) -> AttachmentHashes:
        outcome = self._attachment_outcomes.get(attachment_id, f"{attachment_id:016x}")
        if isinstance(outcome, Exception):
            raise outcome
        # phash is the only field _review_message's downstream store call
        # derives an id from (`f"{hashes.phash:016x}"`); stash the intended
        # hash_id string in phash's hex digits so store_attachment_hash below
        # reproduces it exactly, matching the pre-split fake's behavior.
        return AttachmentHashes(
            attachment_id=attachment_id,
            url=url,
            phash=int(outcome, 16),
            dhash=attachment_id,
            whash=attachment_id,
            ahash=0,
            mphash=0,
            mdhash=0,
            mwhash=0,
            mahash=0,
        )

    async def store_attachment_hash(
        self, guild_id: int, *, hashes: AttachmentHashes, added_by: int
    ) -> GuildHash:
        hash_id = f"{hashes.phash:016x}"
        existing = self.hashes.get(hash_id)
        if existing is not None:
            return existing
        gh = GuildHash(
            hash_id=hash_id,
            phash=hashes.phash,
            dhash=hashes.dhash,
            whash=hashes.whash,
            ahash=hashes.ahash,
            source="reviewmsg",
            added_by=added_by,
        )
        self.hashes[hash_id] = gh
        return gh

    async def get_detection(self, guild_id: int, detection_id: int) -> DetectionFacts | None:
        if self._detection_missing:
            return None
        if detection_id in self.detections:
            return self.detections[detection_id]
        return DetectionFacts(
            detection_id=detection_id,
            channel_id=111,
            message_id=222,
            attachment_id=1,
            uploader_id=333,
            hashes=self._stored_hashes,
        )

    async def set_detection_action(self, guild_id: int, detection_id: int, action: str) -> None:
        self.detection_actions.append((detection_id, action))

    async def set_detection_hashes(
        self, guild_id: int, detection_id: int, hashes: dict[str, int]
    ) -> None:
        self.backfilled_hashes.append((detection_id, hashes))

    async def rest_delete_message(self, channel_id: int, message_id: int) -> bool:
        self.deleted_messages.append((channel_id, message_id))
        return True

    async def rest_ban(
        self, guild_id: int, user_id: int, *, reason: str, purge_seconds: int
    ) -> bool:
        if not self._rest_ban_ok:
            return False
        self.bans.append(
            {"guild_id": guild_id, "user_id": user_id, "reason": reason, "purge": purge_seconds}
        )
        return True

    async def rest_unban(self, guild_id: int, user_id: int, *, reason: str) -> bool:
        if not self._rest_unban_ok:
            return False
        self.unbans.append((guild_id, user_id))
        return True

    async def rest_attachment_url(
        self, channel_id: int, message_id: int, attachment_id: int
    ) -> str | None:
        return self._attachment_url

    async def rest_create_review_channel(
        self, guild_id: int, *, name: str, mod_role_ids: list[int]
    ) -> int | None:
        if self._created_channel_id is None:
            return None
        self.created_channels.append(
            {"guild_id": guild_id, "name": name, "mod_role_ids": mod_role_ids}
        )
        return self._created_channel_id

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
        self.confirmed_scams.append(
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "message_id": message_id,
                "attachment_id": attachment_id,
                "uploader_id": uploader_id,
                "matched_hash_id": matched_hash_id,
            }
        )

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
        self.user_reports.append(
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "message_id": message_id,
                "attachment_id": attachment_id,
                "attachment_url": attachment_url,
                "uploader_id": uploader_id,
                "reporter_id": reporter_id,
            }
        )


def _ctx(command: str, *, perms: int = MANAGE, guild_id: int | None = 1, **opts: Any) -> Any:
    sub = opts.pop("subcommand", None)
    return InteractionContext(
        guild_id=guild_id,
        user_id=99,
        member_permissions=perms,
        command=command,
        subcommand=sub,
        options=opts,
    )


# --- command permission matrix -------------------------------------------------


@pytest.mark.asyncio
async def test_scamhash_denied_without_manage_guild() -> None:
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_ctx("scamhash", perms=NONE, subcommand="list"), FakeDeps())
    assert exc.value.reason is CommandError.NO_PERMISSION


@pytest.mark.asyncio
async def test_scamhash_allowed_with_manage_guild() -> None:
    resp = await handle_command(_ctx("scamhash", perms=MANAGE, subcommand="list"), FakeDeps())
    assert resp.i18n_key == "command.hash_list_empty"


@pytest.mark.asyncio
async def test_admin_satisfies_manage_guild_command() -> None:
    resp = await handle_command(_ctx("scamhash", perms=ADMIN, subcommand="list"), FakeDeps())
    assert resp.i18n_key == "command.hash_list_empty"


@pytest.mark.asyncio
async def test_delete_server_denied_for_manage_guild_only() -> None:
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_ctx("delete_server_data", perms=MANAGE), FakeDeps())
    assert exc.value.reason is CommandError.NO_PERMISSION


@pytest.mark.asyncio
async def test_delete_server_allowed_for_admin() -> None:
    resp = await handle_command(_ctx("delete_server_data", perms=ADMIN), FakeDeps())
    assert resp.i18n_key == "command.delete_server_confirm"


@pytest.mark.asyncio
async def test_guild_only_command_in_dm_rejected() -> None:
    ctx = _ctx("scamhash", perms=ADMIN, guild_id=None, subcommand="list")
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(ctx, FakeDeps())
    assert exc.value.reason is CommandError.GUILD_ONLY


# --- command side effects + audit ----------------------------------------------


@pytest.mark.asyncio
async def test_scamhash_add_hashes_the_image_audits_and_stores() -> None:
    deps = FakeDeps()
    resp = await handle_command(
        _ctx("scamhash", subcommand="add", attachment_id=5, url="https://x/scam.png"), deps
    )
    assert resp.i18n_key == "command.hash_added"
    stored = deps.hashes[f"{5:016x}"]  # FakeDeps hashes to phash == attachment_id
    assert stored.source == "local"
    assert stored.added_by == 99
    assert deps.audits[0][2] == "scamhash.add"


@pytest.mark.asyncio
async def test_scamhash_add_without_resolved_image_is_rejected_gently() -> None:
    """No usable image (wrong file type / nothing resolved) -> guidance, no store."""
    deps = FakeDeps()
    resp = await handle_command(_ctx("scamhash", subcommand="add"), deps)
    assert resp.i18n_key == "command.add_not_image"
    assert not deps.hashes


@pytest.mark.asyncio
async def test_scamhash_add_undecodable_image_reports_reason() -> None:
    deps = FakeDeps(attachment_outcomes={5: AttachmentHashError("bad image")})
    resp = await handle_command(
        _ctx("scamhash", subcommand="add", attachment_id=5, url="https://x/scam.png"), deps
    )
    assert resp.i18n_key == "command.add_fetch_failed"
    assert resp.params["reason"] == "bad image"
    assert not deps.hashes


@pytest.mark.asyncio
async def test_scamhash_add_rate_limited() -> None:
    deps = FakeDeps(hash_rate_ok=False)
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(
            _ctx("scamhash", subcommand="add", attachment_id=5, url="https://x/scam.png"), deps
        )
    assert exc.value.reason is CommandError.RATE_LIMITED


@pytest.mark.asyncio
async def test_config_set_persists_and_audits() -> None:
    deps = FakeDeps()
    resp = await handle_command(
        _ctx("config", subcommand="set", field="retention_days", value="14"), deps
    )
    assert resp.i18n_key == "command.config_set_ok"
    assert deps.config_set == [("retention_days", 14)]
    assert deps.audits[0][2] == "config.set"


@pytest.mark.asyncio
async def test_config_set_review_channel_renders_as_mention() -> None:
    deps = FakeDeps()
    resp = await handle_command(
        _ctx(
            "config",
            subcommand="set",
            field="review_channel",
            value="<#1402357722430570498>",
        ),
        deps,
    )
    assert resp.i18n_key == "command.config_set_ok"
    assert deps.config_set == [("review_channel", 1402357722430570498)]
    # Bare, not in a code span: Discord only substitutes the channel's name for
    # a mention outside backticks, so backticks would show the raw id instead.
    assert resp.params["value"] == "<#1402357722430570498>"


@pytest.mark.asyncio
async def test_config_set_review_channel_clear_renders_as_none() -> None:
    deps = FakeDeps()
    resp = await handle_command(
        _ctx("config", subcommand="set", field="review_channel", value="none"), deps
    )
    assert resp.i18n_key == "command.config_set_ok"
    assert deps.config_set == [("review_channel", None)]
    assert resp.params["value"] == "`none`"


# --- review button auth --------------------------------------------------------


@pytest.mark.asyncio
async def test_review_button_denied_without_manage_guild() -> None:
    ctx = _ctx("", perms=NONE)
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=5)
    with pytest.raises(InteractionRejected) as exc:
        await handle_review_button(ctx, parsed, FakeDeps())
    assert exc.value.reason is CommandError.NO_PERMISSION


@pytest.mark.asyncio
async def test_review_button_unknown_detection_reports_missing() -> None:
    """A forged/cross-guild detection id resolves to nothing and does nothing."""
    deps = FakeDeps(detection_missing=True)
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.detection_missing"
    assert not deps.hashes
    assert not deps.deleted_messages
    assert not deps.audits


@pytest.mark.asyncio
async def test_false_positive_whitelists_unbans_reverses_and_audits() -> None:
    deps = FakeDeps()
    ctx = _ctx("", perms=MANAGE)
    parsed = ParsedCustomId(action=ReviewAction.FALSE_POSITIVE, detection_id=5)
    resp = await handle_review_button(ctx, parsed, deps)
    assert resp.i18n_key == "button.marked_false_positive"
    assert resp.card_note_key == "card.handled"
    assert deps.reversed == [5]
    # The i18n reply says "the image was whitelisted" -- it must actually be.
    assert [w.phash for w in deps.whitelisted] == [0xABC]
    # And an already-banned uploader must actually be freed (best-effort).
    assert deps.unbans == [(1, 333)]
    assert deps.audits[0][2] == "review.false_positive"


@pytest.mark.asyncio
async def test_false_positive_without_image_still_reverses() -> None:
    deps = FakeDeps(stored_hashes=None, attachment_url=None)
    parsed = ParsedCustomId(action=ReviewAction.FALSE_POSITIVE, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.marked_false_positive_no_hash"
    assert deps.reversed == [5]
    assert not deps.whitelisted


@pytest.mark.asyncio
async def test_confirm_scam_blocklists_stored_hashes_and_deletes() -> None:
    deps = FakeDeps()
    ctx = _ctx("", perms=MANAGE)
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=9)
    resp = await handle_review_button(ctx, parsed, deps)
    assert resp.i18n_key == "button.confirmed_scam"
    assert resp.card_note_key == "card.handled"
    # Stored detection hashes -> blocklisted under phash-derived id, no re-fetch.
    stored = deps.hashes[f"{0xABC:016x}"]
    assert stored.source == "review_confirm"
    assert stored.added_by == 99
    assert deps.deleted_messages == [(111, 222)]
    assert deps.detection_actions == [(9, "confirmed")]
    # Hashes were already stored on the row -> no backfill write.
    assert not deps.backfilled_hashes
    assert deps.audits[0] == (1, 99, "review.confirm_scam", "9")


@pytest.mark.asyncio
async def test_confirm_scam_on_member_report_refetches_and_backfills() -> None:
    """Member reports carry no hashes; Confirm re-fetches, hashes, backfills."""
    deps = FakeDeps(stored_hashes=None)
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=9)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.confirmed_scam"
    # FakeDeps.compute_attachment_hashes derives phash from attachment id 1.
    assert f"{1:016x}" in deps.hashes
    # Backfilled so Whitelist/Submit-to-global still work post-delete.
    assert deps.backfilled_hashes == [(9, {"phash": 1, "dhash": 1, "whash": 1, "ahash": 0})]
    assert deps.detection_actions == [(9, "confirmed")]


@pytest.mark.asyncio
async def test_confirm_scam_with_image_gone_still_confirms() -> None:
    deps = FakeDeps(stored_hashes=None, attachment_url=None)
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=9)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.confirmed_no_hash"
    assert not deps.hashes
    assert deps.deleted_messages == [(111, 222)]
    assert deps.detection_actions == [(9, "confirmed")]


@pytest.mark.asyncio
async def test_ban_uploader_bans_with_configured_purge() -> None:
    deps = FakeDeps(config={"ban_purge_hours": 48})
    parsed = ParsedCustomId(action=ReviewAction.BAN_UPLOADER, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.uploader_banned"
    assert deps.bans == [
        {
            "guild_id": 1,
            "user_id": 333,
            "reason": confirmed_reason(5),
            "purge": 48 * 3600,
        }
    ]
    assert deps.detection_actions == [(5, "banned")]
    assert deps.audits[0][2] == "review.ban_uploader"


@pytest.mark.asyncio
async def test_ban_uploader_purge_capped_at_discord_limit() -> None:
    deps = FakeDeps(config={"ban_purge_hours": 9999})
    parsed = ParsedCustomId(action=ReviewAction.BAN_UPLOADER, detection_id=5)
    await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert deps.bans[0]["purge"] == 168 * 3600  # 7 days


@pytest.mark.asyncio
async def test_ban_uploader_rest_refusal_reports_failure() -> None:
    """Role hierarchy / missing perm -> tell the mod, change no state."""
    deps = FakeDeps(rest_ban_ok=False)
    parsed = ParsedCustomId(action=ReviewAction.BAN_UPLOADER, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.action_failed"
    assert not deps.detection_actions
    assert not deps.audits


@pytest.mark.asyncio
async def test_unban_unbans_and_audits() -> None:
    deps = FakeDeps()
    parsed = ParsedCustomId(action=ReviewAction.UNBAN, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.uploader_unbanned"
    assert deps.unbans == [(1, 333)]
    assert deps.audits[0][2] == "review.unban"


@pytest.mark.asyncio
async def test_unban_rest_refusal_reports_failure() -> None:
    deps = FakeDeps(rest_unban_ok=False)
    parsed = ParsedCustomId(action=ReviewAction.UNBAN, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.action_failed"
    assert not deps.audits


@pytest.mark.asyncio
async def test_whitelist_image_adds_whitelist_entry() -> None:
    deps = FakeDeps()
    parsed = ParsedCustomId(action=ReviewAction.WHITELIST_IMAGE, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.image_whitelisted"
    assert [w.phash for w in deps.whitelisted] == [0xABC]
    assert deps.whitelisted[0].added_by == 99
    assert deps.audits[0][2] == "review.whitelist_image"


@pytest.mark.asyncio
async def test_whitelist_image_gone_reports_no_image() -> None:
    deps = FakeDeps(stored_hashes=None, attachment_url=None)
    parsed = ParsedCustomId(action=ReviewAction.WHITELIST_IMAGE, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.no_image"
    assert not deps.whitelisted
    assert not deps.audits


@pytest.mark.asyncio
async def test_legacy_submit_global_button_explains_removal() -> None:
    """Clicks on cards rendered before the redesign get a self-explaining reply."""
    deps = FakeDeps()
    parsed = ParsedCustomId(action=ReviewAction.SUBMIT_GLOBAL, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.submit_global_removed"
    assert not deps.global_votes


# --- Confirm scam as the global vote --------------------------------------------


@pytest.mark.asyncio
async def test_confirm_votes_globally_on_trusted_opted_in_server() -> None:
    deps = FakeDeps(trusted_guilds={1}, config={"optin_global_db": True})
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.confirmed_scam_voted"
    assert deps.global_votes == [
        {
            "hash_id": f"{0xABC:016x}",
            "phash": 0xABC,
            "dhash": 2,
            "whash": 3,
            "voter_user_id": 99,
            "voter_guild_id": 1,
        }
    ]
    assert (1, 99, "global.vote", f"{0xABC:016x}") in deps.audits
    # The local confirm still happened: blocklist + audit + deleted message.
    assert (1, 99, "review.confirm_scam", "5") in deps.audits
    assert f"{0xABC:016x}" in deps.hashes


@pytest.mark.asyncio
async def test_confirm_reports_promotion_when_second_server_agrees() -> None:
    deps = FakeDeps(trusted_guilds={1}, config={"optin_global_db": True}, vote_result="promoted")
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.confirmed_scam_promoted"


@pytest.mark.asyncio
async def test_confirm_stays_local_on_untrusted_server() -> None:
    """Opted-in but NOT allowlisted: the anti-poisoning gate."""
    deps = FakeDeps(config={"optin_global_db": True})
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.confirmed_scam"
    assert not deps.global_votes


@pytest.mark.asyncio
async def test_confirm_stays_local_when_not_opted_in() -> None:
    deps = FakeDeps(trusted_guilds={1})  # allowlisted but optin_global_db is off
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.confirmed_scam"
    assert not deps.global_votes


@pytest.mark.asyncio
async def test_confirm_vote_refusal_keeps_local_confirm() -> None:
    """A refused vote (rate limit / revoked hash) must not fail the confirm."""
    deps = FakeDeps(trusted_guilds={1}, config={"optin_global_db": True}, vote_result=None)
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.confirmed_scam"
    assert deps.global_votes  # attempted
    assert not any(a[2] == "global.vote" for a in deps.audits)  # but not recorded


@pytest.mark.asyncio
async def test_false_positive_revokes_global_entry() -> None:
    deps = FakeDeps(global_hashes={f"{0xABC:016x}"})
    parsed = ParsedCustomId(action=ReviewAction.FALSE_POSITIVE, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.marked_false_positive_global_revoked"
    assert deps.global_disputes == [f"{0xABC:016x}"]
    assert (1, 99, "global.dispute", f"{0xABC:016x}") in deps.audits


@pytest.mark.asyncio
async def test_false_positive_without_global_entry_stays_local() -> None:
    deps = FakeDeps()  # no global entry for this hash
    parsed = ParsedCustomId(action=ReviewAction.FALSE_POSITIVE, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.marked_false_positive"
    assert not any(a[2] == "global.dispute" for a in deps.audits)


# --- appeal lifecycle ----------------------------------------------------------


@pytest.mark.asyncio
async def test_appeal_open_button() -> None:
    deps = FakeDeps()
    ctx = _ctx("", perms=NONE)
    resp = await handle_component(ctx, ComponentAction.APPEAL_OPEN, 77, deps)
    assert resp.i18n_key == "dm.appeal_submitted"
    assert deps.appeals[1]["detection_id"] == 77


@pytest.mark.asyncio
async def test_appeal_open_button_rejects_unowned_detection() -> None:
    # The detection id rides in the forgeable custom id; a user must not be able
    # to open an appeal for a detection that is not theirs (or does not exist).
    deps = FakeDeps(owned_detections=set())
    ctx = _ctx("", perms=NONE)
    resp = await handle_component(ctx, ComponentAction.APPEAL_OPEN, 999, deps)
    assert resp.i18n_key == "command.appeal_none"
    assert not deps.appeals


@pytest.mark.asyncio
async def test_appeal_open_button_rejected_in_dm() -> None:
    deps = FakeDeps()
    ctx = _ctx("", perms=NONE, guild_id=None)
    with pytest.raises(InteractionRejected) as excinfo:
        await handle_component(ctx, ComponentAction.APPEAL_OPEN, 77, deps)
    assert excinfo.value.reason is CommandError.GUILD_ONLY
    assert not deps.appeals


@pytest.mark.asyncio
async def test_appeal_approve_reverses_action() -> None:
    deps = FakeDeps()
    appeal_id = await deps.open_appeal(1, 333, 99)
    ctx = _ctx("", perms=MANAGE)
    resp = await handle_component(ctx, ComponentAction.APPEAL_APPROVE, appeal_id, deps)
    assert resp.i18n_key == "button.appeal_approved"
    assert deps.resolved == [(appeal_id, True)]
    assert deps.reversed == [333]
    # Approval must actually lift the enforcement: best-effort unban of the
    # appellant (open_appeal above filed it for user 99).
    assert deps.unbans == [(1, 99)]


@pytest.mark.asyncio
async def test_appeal_deny_does_not_reverse() -> None:
    deps = FakeDeps()
    appeal_id = await deps.open_appeal(1, 333, 99)
    ctx = _ctx("", perms=MANAGE)
    resp = await handle_component(ctx, ComponentAction.APPEAL_DENY, appeal_id, deps)
    assert resp.i18n_key == "button.appeal_denied"
    assert deps.resolved == [(appeal_id, False)]
    assert deps.reversed == []


@pytest.mark.asyncio
async def test_appeal_approve_denied_without_permission() -> None:
    deps = FakeDeps()
    ctx = _ctx("", perms=NONE)
    with pytest.raises(InteractionRejected) as exc:
        await handle_component(ctx, ComponentAction.APPEAL_APPROVE, 1, deps)
    assert exc.value.reason is CommandError.NO_PERMISSION


# --- safe-mode + purge components ----------------------------------------------


@pytest.mark.asyncio
async def test_safe_mode_resume_requires_manage_guild() -> None:
    deps = FakeDeps()
    with pytest.raises(InteractionRejected):
        await handle_component(_ctx("", perms=NONE), ComponentAction.SAFE_MODE_RESUME, 1, deps)
    resp = await handle_component(_ctx("", perms=MANAGE), ComponentAction.SAFE_MODE_RESUME, 1, deps)
    assert resp.i18n_key == "button.safe_mode_resumed"
    assert deps.safe_mode_disabled == [1]


@pytest.mark.asyncio
async def test_delete_confirm_requires_admin_and_purges() -> None:
    deps = FakeDeps()
    confirm = ComponentAction.DELETE_SERVER_CONFIRM
    with pytest.raises(InteractionRejected):
        await handle_component(_ctx("", perms=MANAGE), confirm, 1, deps)
    resp = await handle_component(
        _ctx("", perms=ADMIN), ComponentAction.DELETE_SERVER_CONFIRM, 1, deps
    )
    assert resp.i18n_key == "command.delete_server_ok"
    assert deps.purged == [1]


# --- scamhash remove/list/import/export ----------------------------------------


@pytest.mark.asyncio
async def test_scamhash_remove_found_audits() -> None:
    deps = FakeDeps()
    deps.hashes["abc"] = GuildHash(
        hash_id="abc", phash=1, dhash=2, whash=3, ahash=0, source="local"
    )
    resp = await handle_command(_ctx("scamhash", subcommand="remove", hash_id="abc"), deps)
    assert resp.i18n_key == "command.hash_removed"
    assert deps.audits[0][2] == "scamhash.remove"


@pytest.mark.asyncio
async def test_scamhash_remove_not_found_does_not_audit() -> None:
    deps = FakeDeps()
    resp = await handle_command(_ctx("scamhash", subcommand="remove", hash_id="missing"), deps)
    assert resp.i18n_key == "command.hash_not_found"
    assert deps.audits == []


@pytest.mark.asyncio
async def test_scamhash_list_non_empty() -> None:
    deps = FakeDeps()
    for hash_id in ("def", "abc"):
        deps.hashes[hash_id] = GuildHash(
            hash_id=hash_id, phash=1, dhash=2, whash=3, ahash=0, source="local", added_by=42
        )
    resp = await handle_command(_ctx("scamhash", subcommand="list"), deps)
    assert resp.i18n_key == "command.hash_list_header"
    # Entries must actually be rendered (the header alone showed an empty list
    # after the colon in production), sorted, with source and adder attribution.
    assert render(resp, "en") == (
        "This server has 2 scam hash(es):\n"
        "\u2022 `abc` \u2014 local by <@42>\n"
        "\u2022 `def` \u2014 local by <@42>"
    )


@pytest.mark.asyncio
async def test_scamhash_list_truncates_to_fit_discord_reply() -> None:
    deps = FakeDeps()
    for value in range(_HASH_LIST_PREVIEW_LIMIT + 1):
        # Worst-case rows: full 64-char hash ids, max-width source column, and
        # a full-width snowflake mention on every line.
        hash_id = f"{value:064x}"
        deps.hashes[hash_id] = GuildHash(
            hash_id=hash_id,
            phash=value,
            dhash=0,
            whash=0,
            ahash=0,
            source="x" * 32,
            added_by=2**63,
        )

    resp = await handle_command(_ctx("scamhash", subcommand="list"), deps)
    message = render(resp, "en")

    assert resp.i18n_key == "command.hash_list_truncated"
    assert resp.params["count"] == _HASH_LIST_PREVIEW_LIMIT + 1
    assert f"`{_HASH_LIST_PREVIEW_LIMIT - 1:064x}`" in message
    assert f"`{_HASH_LIST_PREVIEW_LIMIT:064x}`" not in message
    assert "1 more" in message
    assert len(message) <= 2000


@pytest.mark.asyncio
async def test_scamhash_import_stores_and_dedupes() -> None:
    deps = FakeDeps()
    doc = (
        '{"version": 1, "hashes": ['
        '{"phash": 10, "dhash": 0, "whash": 0},'
        '{"phash": 10, "dhash": 0, "whash": 0},'  # duplicate phash -> skipped
        '{"phash": 20, "dhash": 0, "whash": 0}]}'
    )
    resp = await handle_command(_ctx("scamhash", subcommand="import", file=doc), deps)
    assert resp.i18n_key == "command.import_ok"
    assert resp.params["added"] == 2
    assert resp.params["skipped"] == 1
    assert len(deps.hashes) == 2
    assert deps.audits[0][2] == "scamhash.import"


@pytest.mark.asyncio
async def test_scamhash_export_roundtrips() -> None:
    deps = FakeDeps()
    deps.hashes["abc"] = GuildHash(
        hash_id="abc", phash=7, dhash=8, whash=9, ahash=0, source="local"
    )
    resp = await handle_command(_ctx("scamhash", subcommand="export"), deps)
    assert resp.i18n_key == "command.export_ok"
    assert resp.params["count"] == 1
    assert resp.attachment is not None and '"phash":7' in resp.attachment


@pytest.mark.asyncio
async def test_scamhash_export_with_no_hashes_explains_instead_of_empty_file() -> None:
    resp = await handle_command(_ctx("scamhash", subcommand="export"), FakeDeps())
    assert resp.i18n_key == "command.export_empty"
    assert resp.attachment is None


# --- config view + stats -------------------------------------------------------


@pytest.mark.asyncio
async def test_config_view() -> None:
    resp = await handle_command(_ctx("config", subcommand="view"), FakeDeps())
    assert resp.i18n_key == "command.config_view_header"
    # FakeDeps.get_config's default {"locale": "en"} still renders a summary line.
    assert "**locale**: `en`" in resp.params["summary"]


@pytest.mark.asyncio
async def test_config_view_renders_review_channel_as_mention() -> None:
    deps = FakeDeps()
    deps.get_config = _fake_get_config_with_channel  # type: ignore[method-assign]
    resp = await handle_command(_ctx("config", subcommand="view"), deps)
    assert "<#1402357722430570498>" in resp.params["summary"]
    # A mention inside a code span renders as the literal id, which is the raw
    # number a moderator would then have to look up by hand.
    assert "`<#1402357722430570498>`" not in resp.params["summary"]


@pytest.mark.asyncio
async def test_config_set_field_values_stay_quoted() -> None:
    """Non-channel values keep their backticks: they are literal input text."""
    deps = FakeDeps()
    resp = await handle_command(
        _ctx("config", subcommand="set", field="retention_days", value="14"), deps
    )
    assert resp.params["value"] == "`14`"


@pytest.mark.asyncio
async def test_config_permissions_reports_blocked_channels() -> None:
    deps = FakeDeps(
        access_report=build_access_report([(10, VIEW_CHANNEL | MANAGE_MESSAGES), (11, 0)])
    )
    resp = await handle_command(_ctx("config", subcommand="permissions"), deps)
    assert resp.i18n_key == "command.permissions_report"
    assert "<#11>" in resp.params["report"]


@pytest.mark.asyncio
async def test_config_permissions_says_so_when_it_cannot_check() -> None:
    """An unanswerable check must not read as a clean bill of health."""
    resp = await handle_command(_ctx("config", subcommand="permissions"), FakeDeps())
    assert resp.i18n_key == "command.permissions_unknown"


@pytest.mark.asyncio
async def test_config_permissions_requires_manage_guild() -> None:
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_ctx("config", subcommand="permissions", perms=NONE), FakeDeps())
    assert exc.value.reason is CommandError.NO_PERMISSION


async def _fake_get_config_with_channel(guild_id: int) -> dict[str, Any]:
    return {"locale": "en", "review_channel": 1402357722430570498}


@pytest.mark.asyncio
async def test_config_view_no_row_shows_defaults_message() -> None:
    deps = FakeDeps()
    deps.get_config = _fake_get_config_empty  # type: ignore[method-assign]
    resp = await handle_command(_ctx("config", subcommand="view"), deps)
    assert "defaults are in effect" in resp.params["summary"]


async def _fake_get_config_empty(guild_id: int) -> dict[str, Any]:
    return {}


@pytest.mark.parametrize("field", _CONFIG_VIEW_ORDER)
def test_every_config_view_field_is_settable_under_the_same_name(field: str) -> None:
    """Every field /config view can display must be settable via /config set
    under that exact same name -- guards against the DB-column-name leak this
    regressed as for "review_channel" (view said "review_channel_id").

    Uses each field's already-valid value from FakeDeps' default config shape
    so this only checks *name* recognition, not value validation (that's
    covered by test_validate_config_set_valid/_invalid_value already).
    """
    from optimus.services.interactions.logic import validate_config_set

    sample_values = {
        "sensitivity": "balanced",
        "action_policy": "report_only",
        "mod_queue_threshold": "0.5",
        "review_channel": "none",
        "ban_purge_hours": "24",
        "safe_mode": "false",
        "retention_days": "30",
        "locale": "en",
        "optin_global_db": "false",
        "optin_scan_bots": "false",
        "optin_evidence_storage": "false",
    }
    assert field in sample_values, f"add a sample value for new config field {field!r}"
    # Must not raise InteractionRejected(UNKNOWN_FIELD) -- i.e. the name itself
    # is recognized. A ValueError here would mean the sample value is wrong,
    # which is a test bug, not a production one.
    validate_config_set(field, sample_values[field])


@pytest.mark.asyncio
async def test_config_set_field_name_matches_config_view_field_name() -> None:
    """Regression test: /config set field:<X> and /config view must agree on
    the name <X> for every field, so a name copied from one always works
    verbatim in the other. This previously drifted for the review channel:
    /config set accepted "review_channel" but /config view rendered it back
    as "review_channel_id" (the raw DB column name), so a user reading
    /config view's output and pasting it into /config set got an "Unknown
    configuration field" rejection.
    """
    store: dict[str, Any] = {"locale": "en"}
    deps = FakeDeps()

    async def fake_get_config(guild_id: int) -> dict[str, Any]:
        return dict(store)

    async def fake_set_config_field(guild_id: int, field: str, value: Any) -> None:
        store[field] = value
        deps.config_set.append((field, value))

    deps.get_config = fake_get_config  # type: ignore[method-assign]
    deps.set_config_field = fake_set_config_field  # type: ignore[method-assign]

    set_resp = await handle_command(
        _ctx(
            "config",
            subcommand="set",
            field="review_channel",
            value="<#1402357722430570498>",
        ),
        deps,
    )
    assert set_resp.i18n_key == "command.config_set_ok"

    view_resp = await handle_command(_ctx("config", subcommand="view"), deps)
    # Unquoted on purpose: backticks would leave Discord showing the raw id.
    assert "**review_channel**: <#1402357722430570498>" in view_resp.params["summary"]
    assert "review_channel_id" not in view_resp.params["summary"]


# --- /setup: provision or link the shared review channel -----------------------


@pytest.mark.asyncio
async def test_setup_creates_private_channel_and_links_it() -> None:
    deps = FakeDeps()
    resp = await handle_command(_ctx("setup", mod_role=777), deps)
    assert resp.i18n_key == "command.setup_created"
    assert resp.params == {"channel_id": 555}
    assert deps.created_channels == [
        {"guild_id": 1, "name": "optimus-review", "mod_role_ids": [777]}
    ]
    assert ("review_channel", 555) in deps.config_set
    assert deps.audits[0][2] == "setup.review_channel"


@pytest.mark.asyncio
async def test_setup_without_role_creates_admin_only_channel() -> None:
    deps = FakeDeps()
    resp = await handle_command(_ctx("setup"), deps)
    # Different reply key: it must warn that only admins + the bot can see it.
    assert resp.i18n_key == "command.setup_created_no_role"
    assert deps.created_channels[0]["mod_role_ids"] == []
    assert ("review_channel", 555) in deps.config_set


@pytest.mark.asyncio
async def test_setup_links_existing_channel_without_creating() -> None:
    deps = FakeDeps()
    resp = await handle_command(_ctx("setup", channel=888), deps)
    assert resp.i18n_key == "command.setup_linked"
    assert resp.params == {"channel_id": 888}
    assert not deps.created_channels
    assert ("review_channel", 888) in deps.config_set


@pytest.mark.asyncio
async def test_setup_with_channel_option_repoints_over_existing() -> None:
    deps = FakeDeps(config={"review_channel": 444})
    resp = await handle_command(_ctx("setup", channel=888), deps)
    assert resp.i18n_key == "command.setup_linked"
    assert ("review_channel", 888) in deps.config_set


@pytest.mark.asyncio
async def test_setup_rerun_reports_existing_channel_instead_of_duplicating() -> None:
    deps = FakeDeps(config={"review_channel": 444})
    resp = await handle_command(_ctx("setup"), deps)
    assert resp.i18n_key == "command.setup_already"
    assert resp.params == {"channel_id": 444}
    assert not deps.created_channels
    assert not deps.config_set


@pytest.mark.asyncio
async def test_setup_rest_refusal_reports_failure_without_writes() -> None:
    deps = FakeDeps(created_channel_id=None)
    resp = await handle_command(_ctx("setup"), deps)
    assert resp.i18n_key == "command.setup_failed"
    assert not deps.config_set
    assert not deps.audits


@pytest.mark.asyncio
async def test_setup_requires_manage_guild() -> None:
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_ctx("setup", perms=NONE), FakeDeps())
    assert exc.value.reason is CommandError.NO_PERMISSION


@pytest.mark.asyncio
async def test_stats_non_empty() -> None:
    resp = await handle_command(_ctx("stats"), FakeDeps())
    assert resp.i18n_key == "command.stats_header"
    assert resp.params["hours"] == 24
    assert "detections" in resp.params  # header renders a body, not a bare colon
    # The persistence canary is rendered for moderators to eyeball after deploys.
    assert render(resp, "en") == (
        "Statistics for the last 24 hours:\n"
        "\u2022 Detections: 3\n"
        "\u2022 Database: boot #5, storing data since 2026-08-01"
    )


@pytest.mark.asyncio
async def test_stats_zero_detections_still_shows_persistence_canary() -> None:
    deps = FakeDeps()

    async def _quiet(guild_id: int) -> dict[str, Any]:
        return {"detections": 0, "hours": 24, "boots": 2, "first_boot": "2026-08-01"}

    deps.stats_summary = _quiet  # type: ignore[method-assign]
    resp = await handle_command(_ctx("stats"), deps)
    # A quiet server must still surface the boot counter — that's the whole
    # point of the canary: verify persistence even when nothing was detected.
    assert resp.i18n_key == "command.stats_header"
    assert resp.params["boots"] == 2


def _load_deps(**load: int) -> FakeDeps:
    """FakeDeps whose stats_summary carries pipeline-load numbers."""
    deps = FakeDeps()

    async def _summary(guild_id: int) -> dict[str, Any]:
        return {"detections": 3, "hours": 24, "boots": 5, "first_boot": "2026-08-01", **load}

    deps.stats_summary = _summary  # type: ignore[method-assign]
    return deps


@pytest.mark.asyncio
async def test_stats_renders_pipeline_load_with_skip_breakdown() -> None:
    """Moderators can see whether the bot is keeping up without /metrics."""
    deps = _load_deps(
        scanned=128431,
        queued=2,
        skipped=3104,
        duplicates=2890,
        rejected=180,
        rate_limited=29,
        dropped=5,
    )
    resp = await handle_command(_ctx("stats"), deps)
    body = render(resp, "en")
    # Thousands separators: a raw 128431 is unreadable at a glance.
    assert "\u2022 Images scanned: 128,431" in body
    assert "\u2022 Waiting on moderation: 2" in body
    assert "\u2022 Skipped: 3,104" in body
    # The breakdown is Discord subtext so the headline numbers stay dominant.
    assert "-# already seen 2,890" in body
    assert "rate-limited 29" in body
    # Never presented as this server's own traffic -- the counters have no
    # guild label and cannot be attributed to one server.
    assert "every server this bot is in" in body
    # The guild-scoped section is untouched.
    assert body.startswith("Statistics for the last 24 hours:")


@pytest.mark.asyncio
async def test_stats_omits_skip_breakdown_when_nothing_was_skipped() -> None:
    """A healthy pipeline must not print four zeroes on every invocation."""
    deps = _load_deps(scanned=412, queued=0, skipped=0)
    body = render(await handle_command(_ctx("stats"), deps), "en")
    assert "\u2022 Skipped: 0" in body
    assert "already seen" not in body


@pytest.mark.asyncio
async def test_stats_omits_load_section_entirely_on_a_fresh_process() -> None:
    """Right after a restart every counter is zero and the section says
    nothing actionable, so it is dropped rather than shown as all zeroes."""
    deps = _load_deps(scanned=0, queued=0, skipped=0)
    resp = await handle_command(_ctx("stats"), deps)
    assert resp.params["load"] == ""
    body = render(resp, "en")
    assert "Pipeline load" not in body
    # Falling back to the pre-existing output exactly, with no dangling blank
    # lines where the section would have been.
    assert body == (
        "Statistics for the last 24 hours:\n"
        "\u2022 Detections: 3\n"
        "\u2022 Database: boot #5, storing data since 2026-08-01"
    )


@pytest.mark.asyncio
async def test_stats_load_section_is_localized() -> None:
    deps = _load_deps(scanned=10, queued=1, skipped=2, duplicates=2)
    ctx = InteractionContext(
        guild_id=1,
        user_id=99,
        member_permissions=MANAGE,
        command="stats",
        subcommand=None,
        options={},
        locale="sr",
    )
    body = render(await handle_command(ctx, deps), "sr")
    assert "Skenirano slika: 10" in body
    assert "ve\u0107 vi\u0111eno 2" in body
    # No English leaked through from the sub-key assembled in the handler.
    assert "already seen" not in body


# --- /global (owner-only allowlist) and /help ----------------------------------


@pytest.mark.asyncio
async def test_global_refuses_non_owner() -> None:
    deps = FakeDeps(owner_ids={1000})  # ctx.user_id is 99
    resp = await handle_command(_ctx("global", subcommand="servers"), deps)
    assert resp.i18n_key == "command.owner_only"


@pytest.mark.asyncio
async def test_global_fails_closed_when_owner_lookup_fails() -> None:
    """An empty owner set (REST error) must refuse — never grant trust blindly."""
    deps = FakeDeps(owner_ids=set())
    resp = await handle_command(_ctx("global", subcommand="approve_server", server_id="2"), deps)
    assert resp.i18n_key == "command.owner_only"
    assert not deps.trusted_guilds


@pytest.mark.asyncio
async def test_global_approve_and_revoke_server() -> None:
    deps = FakeDeps(owner_ids={99})
    resp = await handle_command(_ctx("global", subcommand="approve_server", server_id="123"), deps)
    assert resp.i18n_key == "command.global_server_approved"
    assert deps.trusted_guilds == {123}
    assert (1, 99, "global.approve_server", "123") in deps.audits

    resp = await handle_command(_ctx("global", subcommand="approve_server", server_id="123"), deps)
    assert resp.i18n_key == "command.global_server_already"

    resp = await handle_command(_ctx("global", subcommand="revoke_server", server_id="123"), deps)
    assert resp.i18n_key == "command.global_server_revoked"
    assert not deps.trusted_guilds
    assert (1, 99, "global.revoke_server", "123") in deps.audits

    resp = await handle_command(_ctx("global", subcommand="revoke_server", server_id="123"), deps)
    assert resp.i18n_key == "command.global_server_missing"


@pytest.mark.asyncio
async def test_global_servers_listing() -> None:
    deps = FakeDeps(owner_ids={99})
    resp = await handle_command(_ctx("global", subcommand="servers"), deps)
    assert resp.i18n_key == "command.global_servers_none"

    deps.trusted_guilds.update({5, 3})
    resp = await handle_command(_ctx("global", subcommand="servers"), deps)
    assert resp.i18n_key == "command.global_servers"
    assert resp.params == {"count": 2, "listing": "\u2022 `3`\n\u2022 `5`"}


@pytest.mark.asyncio
async def test_global_rejects_non_numeric_server_id() -> None:
    deps = FakeDeps(owner_ids={99})
    resp = await handle_command(
        _ctx("global", subcommand="approve_server", server_id="my-server"), deps
    )
    assert resp.i18n_key == "command.global_invalid_server"
    assert not deps.trusted_guilds


@pytest.mark.asyncio
async def test_help_is_available_to_everyone() -> None:
    deps = FakeDeps()
    resp = await handle_command(_ctx("help", perms=NONE), deps)
    assert resp.i18n_key == "command.help"
    # The rendered text must exist in every locale and mention the guide link.
    assert "github.com/rafs2006/optimus" in render(resp, "en")
    assert "github.com/rafs2006/optimus" in render(resp, "sr")


# --- /scamhash reviewmsg and the "Review as scam" context-menu entry ----------


def _review_ctx(
    *,
    command: str = "scamhash",
    subcommand: str | None = "review",
    attachments: list[tuple[int, str]] | None = None,
    channel_id: int = 111,
    message_id: int = 222,
    author_id: int = 333,
    perms: int = MANAGE,
) -> InteractionContext:
    return InteractionContext(
        guild_id=1,
        user_id=99,
        member_permissions=perms,
        command=command,
        subcommand=subcommand,
        options={
            "channel_id": channel_id,
            "message_id": message_id,
            "author_id": author_id,
            "attachments": attachments if attachments is not None else [(1, "https://x/1.png")],
        },
    )


@pytest.mark.asyncio
async def test_reviewmsg_denied_without_manage_guild() -> None:
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_review_ctx(perms=NONE), FakeDeps())
    assert exc.value.reason is CommandError.NO_PERMISSION


@pytest.mark.asyncio
async def test_reviewmsg_rate_limited_rejected() -> None:
    deps = FakeDeps(hash_rate_ok=False)
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_review_ctx(), deps)
    assert exc.value.reason is CommandError.RATE_LIMITED


@pytest.mark.asyncio
async def test_reviewmsg_no_images_short_circuits() -> None:
    deps = FakeDeps()
    resp = await handle_command(_review_ctx(attachments=[]), deps)
    assert resp.i18n_key == "command.reviewmsg_no_images"
    assert deps.confirmed_scams == []


@pytest.mark.asyncio
async def test_reviewmsg_single_attachment_hashes_and_actions_author() -> None:
    deps = FakeDeps()
    resp = await handle_command(
        _review_ctx(attachments=[(1, "https://x/1.png")], author_id=333), deps
    )
    assert resp.i18n_key == "command.reviewmsg_result_report_only"
    assert resp.params == {"added": 1, "failed": 0, "author_id": 333, "action": "report_only"}
    assert len(deps.confirmed_scams) == 1
    assert deps.confirmed_scams[0]["uploader_id"] == 333
    assert deps.confirmed_scams[0]["attachment_id"] == 1
    assert len(deps.audits) == 1
    assert deps.audits[0][2] == "scamhash.reviewmsg"


@pytest.mark.asyncio
async def test_reviewmsg_multiple_attachments_all_succeed() -> None:
    deps = FakeDeps()
    attachments = [(1, "https://x/1.png"), (2, "https://x/2.png"), (3, "https://x/3.png")]
    resp = await handle_command(_review_ctx(attachments=attachments), deps)
    assert resp.i18n_key == "command.reviewmsg_result_report_only"
    assert resp.params["added"] == 3
    assert resp.params["failed"] == 0
    assert len(deps.confirmed_scams) == 3
    assert len(deps.hashes) == 3


@pytest.mark.asyncio
async def test_reviewmsg_some_attachments_fail_are_skipped() -> None:
    deps = FakeDeps(attachment_outcomes={2: AttachmentHashError("bad image")})
    attachments = [(1, "https://x/1.png"), (2, "https://x/2.png"), (3, "https://x/3.png")]
    resp = await handle_command(_review_ctx(attachments=attachments), deps)
    assert resp.i18n_key == "command.reviewmsg_result_report_only"
    assert resp.params["added"] == 2
    assert resp.params["failed"] == 1
    # The failed attachment never reaches the moderation pipeline.
    assert {c["attachment_id"] for c in deps.confirmed_scams} == {1, 3}


@pytest.mark.asyncio
async def test_reviewmsg_all_attachments_fail() -> None:
    deps = FakeDeps(
        attachment_outcomes={
            1: AttachmentHashError("bad"),
            2: AttachmentHashError("bad"),
        }
    )
    attachments = [(1, "https://x/1.png"), (2, "https://x/2.png")]
    resp = await handle_command(_review_ctx(attachments=attachments), deps)
    assert resp.i18n_key == "command.reviewmsg_all_failed"
    assert resp.params == {"failed": 2}
    assert deps.confirmed_scams == []


@pytest.mark.asyncio
async def test_reviewmsg_duplicate_hash_is_idempotent() -> None:
    """Re-reviewing a message (or an image already hashed by another path)
    must not raise -- ``store_attachment_hash`` returns the existing row."""
    deps = FakeDeps()
    existing = GuildHash(hash_id=f"{1:016x}", phash=1, dhash=1, whash=1, ahash=0, source="local")
    deps.hashes[existing.hash_id] = existing
    resp = await handle_command(_review_ctx(attachments=[(1, "https://x/1.png")]), deps)
    assert resp.i18n_key == "command.reviewmsg_result_report_only"
    assert resp.params["added"] == 1
    assert len(deps.confirmed_scams) == 1
    assert deps.confirmed_scams[0]["matched_hash_id"] == existing.hash_id


@pytest.mark.asyncio
async def test_reviewmsg_computes_all_hashes_before_storing_any() -> None:
    """All ``compute_attachment_hashes`` calls (network fetch + decode -- no DB)
    must happen before any ``store_attachment_hash``/``audit``/
    ``submit_confirmed_scam`` call (DB writes).

    This is the actual behavioral guarantee behind splitting
    ``hash_and_store_attachment`` into a compute phase and a store phase: a
    multi-image review must never interleave a slow network+decode call for
    one attachment with a DB write for another, because the whole handler
    runs inside one caller-managed transaction (see
    :meth:`InteractionService._run`) and a SQLite write transaction holds an
    exclusive file-level lock for as long as it stays open. Interleaving used
    to hold that lock open for the sum of every attachment's fetch+decode
    time plus every write, which could starve a concurrent writer into a
    "database is locked" error well past any reasonable retry budget.
    """
    calls: list[str] = []

    class OrderTrackingDeps(FakeDeps):
        async def compute_attachment_hashes(
            self, *, attachment_id: int, url: str
        ) -> AttachmentHashes:
            calls.append(f"compute:{attachment_id}")
            return await super().compute_attachment_hashes(attachment_id=attachment_id, url=url)

        async def store_attachment_hash(
            self, guild_id: int, *, hashes: AttachmentHashes, added_by: int
        ) -> GuildHash:
            calls.append(f"store:{hashes.attachment_id}")
            return await super().store_attachment_hash(guild_id, hashes=hashes, added_by=added_by)

        async def audit(
            self, guild_id: int, actor_id: int, action: str, *, target: str | None = None
        ) -> None:
            calls.append("audit")
            await super().audit(guild_id, actor_id, action, target=target)

        async def submit_confirmed_scam(self, guild_id: int, **kwargs: Any) -> None:
            calls.append(f"submit:{kwargs['attachment_id']}")
            await super().submit_confirmed_scam(guild_id, **kwargs)

    deps = OrderTrackingDeps()
    attachments = [(1, "https://x/1.png"), (2, "https://x/2.png"), (3, "https://x/3.png")]
    resp = await handle_command(_review_ctx(attachments=attachments), deps)

    assert resp.params["added"] == 3
    compute_calls = [c for c in calls if c.startswith("compute:")]
    other_calls = [c for c in calls if not c.startswith("compute:")]
    assert compute_calls == ["compute:1", "compute:2", "compute:3"]
    assert calls.index(other_calls[0]) > calls.index(compute_calls[-1]), (
        "a store/audit/submit call happened before all attachments were computed"
    )


@pytest.mark.asyncio
async def test_reviewmsg_reply_reports_configured_action_when_policy_acts() -> None:
    """With an acting policy the reply must state the submitted action -- and with
    a non-acting config the reply must never claim the author was "actioned".
    The old unconditional reviewmsg_result told moderators the message was
    handled even on servers whose policy meant the bot deliberately did
    nothing (the default!), sending them hunting for a delivery bug that was
    actually a config setting."""
    deps = FakeDeps(config={"action_policy": "delete_ban", "review_channel": 444})
    resp = await handle_command(
        _review_ctx(attachments=[(1, "https://x/1.png")], author_id=333), deps
    )
    assert resp.i18n_key == "command.reviewmsg_result_submitted"
    assert resp.params["action"] == "delete_ban"
    assert resp.params["author_id"] == 333
    message = render(resp, "en")
    assert "Submitted delete_ban" in message
    assert "Queued" not in message
    assert "<#444>" in message
    assert "final outcome" in message
    # The verdict still reaches the pipeline regardless of the reply wording.
    assert len(deps.confirmed_scams) == 1


@pytest.mark.asyncio
async def test_reviewmsg_reply_explains_async_outcome_without_review_channel() -> None:
    deps = FakeDeps(config={"action_policy": "delete_ban"})
    resp = await handle_command(_review_ctx(), deps)

    assert resp.i18n_key == "command.reviewmsg_result_submitted_no_channel"
    message = render(resp, "en")
    assert "Submitted delete_ban" in message
    assert "asynchronously" in message
    assert "does not show its final outcome" in message


@pytest.mark.asyncio
async def test_reviewmsg_reply_says_report_only_under_report_only_policy() -> None:
    deps = FakeDeps(config={"action_policy": "report_only"})
    resp = await handle_command(_review_ctx(attachments=[(1, "https://x/1.png")]), deps)
    assert resp.i18n_key == "command.reviewmsg_result_report_only"
    assert resp.params["action"] == "report_only"
    assert len(deps.confirmed_scams) == 1


@pytest.mark.asyncio
async def test_reviewmsg_reply_says_safe_mode_even_with_acting_policy() -> None:
    """Safe mode overrides the configured action in policy.decide, so the
    reply must lead with safe mode, not the (suppressed) acting policy."""
    deps = FakeDeps(config={"action_policy": "delete_ban", "safe_mode": True})
    resp = await handle_command(_review_ctx(attachments=[(1, "https://x/1.png")]), deps)
    assert resp.i18n_key == "command.reviewmsg_result_safe_mode"
    assert len(deps.confirmed_scams) == 1


@pytest.mark.asyncio
async def test_context_menu_review_message_routes_to_shared_core() -> None:
    """The context-menu entry point (``review_message``) shares ``_review_message``
    with the slash command -- both are gated by the same permission and both
    read the glue-resolved options in the same shape."""
    deps = FakeDeps()
    ctx = _review_ctx(command="review_message", subcommand=None)
    resp = await handle_command(ctx, deps)
    assert resp.i18n_key == "command.reviewmsg_result_report_only"
    assert len(deps.confirmed_scams) == 1


@pytest.mark.asyncio
async def test_context_menu_review_message_denied_without_manage_guild() -> None:
    ctx = _review_ctx(command="review_message", subcommand=None, perms=NONE)
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(ctx, FakeDeps())
    assert exc.value.reason is CommandError.NO_PERMISSION


# --- "Report scam to mods" member context-menu command --------------------------


@pytest.mark.asyncio
async def test_report_message_files_review_and_audits() -> None:
    deps = FakeDeps()
    ctx = _review_ctx(command="report_message", subcommand=None, perms=NONE)
    resp = await handle_command(ctx, deps)
    assert resp.i18n_key == "command.report_ok"
    assert deps.user_reports == [
        {
            "guild_id": 1,
            "channel_id": 111,
            "message_id": 222,
            "attachment_id": 1,
            # Carried so the review card can render the reported image.
            "attachment_url": "https://x/1.png",
            "uploader_id": 333,
            "reporter_id": 99,
        }
    ]
    assert deps.audits == [(1, 99, "report.message", "222")]
    # A member report never touches the blocklist or confirmed verdicts.
    assert deps.hashes == {}
    assert deps.confirmed_scams == []


@pytest.mark.asyncio
async def test_report_message_requires_no_permission() -> None:
    """Unlike review_message, a member with zero permissions can report."""
    deps = FakeDeps()
    resp = await handle_command(
        _review_ctx(command="report_message", subcommand=None, perms=NONE), deps
    )
    assert resp.i18n_key == "command.report_ok"


@pytest.mark.asyncio
async def test_report_message_without_images_short_circuits() -> None:
    deps = FakeDeps()
    ctx = _review_ctx(command="report_message", subcommand=None, perms=NONE, attachments=[])
    resp = await handle_command(ctx, deps)
    assert resp.i18n_key == "command.report_no_images"
    assert deps.user_reports == []


@pytest.mark.asyncio
async def test_report_message_rate_limited() -> None:
    deps = FakeDeps(report_rate_ok=False)
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_review_ctx(command="report_message", subcommand=None), deps)
    assert exc.value.reason is CommandError.RATE_LIMITED
    assert deps.user_reports == []


@pytest.mark.asyncio
async def test_report_message_guild_only() -> None:
    ctx = InteractionContext(
        guild_id=None,
        user_id=99,
        member_permissions=0,
        command="report_message",
        subcommand=None,
        options={"channel_id": 111, "message_id": 222, "author_id": 333, "attachments": []},
    )
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(ctx, FakeDeps())
    assert exc.value.reason is CommandError.GUILD_ONLY


# --- /report slash command (same handler, typed target) -------------------------


@pytest.mark.asyncio
async def test_report_slash_command_files_the_same_review() -> None:
    """``/report`` reaches the identical handler as the context menu.

    The glue layer resolves ``message:<link-or-id>`` into the same
    channel/message/author/attachments option shape, so the two surfaces are
    indistinguishable from here down -- no second code path to keep in sync.
    """
    deps = FakeDeps()
    ctx = _review_ctx(command="report", subcommand=None, perms=NONE)
    resp = await handle_command(ctx, deps)
    assert resp.i18n_key == "command.report_ok"
    assert deps.user_reports == [
        {
            "guild_id": 1,
            "channel_id": 111,
            "message_id": 222,
            "attachment_id": 1,
            "attachment_url": "https://x/1.png",
            "uploader_id": 333,
            "reporter_id": 99,
        }
    ]
    assert deps.audits == [(1, 99, "report.message", "222")]
    # Still inert: no hash stored, no confirmed verdict submitted.
    assert deps.hashes == {}
    assert deps.confirmed_scams == []


@pytest.mark.asyncio
async def test_report_slash_command_is_rate_limited_like_the_menu() -> None:
    """The typed surface must not be a way around the per-user report limit."""
    deps = FakeDeps(report_rate_ok=False)
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_review_ctx(command="report", subcommand=None, perms=NONE), deps)
    assert exc.value.reason is CommandError.RATE_LIMITED
    assert deps.user_reports == []


@pytest.mark.asyncio
async def test_report_slash_command_without_images_short_circuits() -> None:
    deps = FakeDeps()
    ctx = _review_ctx(command="report", subcommand=None, perms=NONE, attachments=[])
    resp = await handle_command(ctx, deps)
    assert resp.i18n_key == "command.report_no_images"
    assert deps.user_reports == []


@pytest.mark.asyncio
async def test_config_view_explains_every_field() -> None:
    """Each rendered field carries its `-#` subtext explanation so mods learn
    what a setting does (and its default) without leaving Discord."""
    full_config = {
        "sensitivity": "balanced",
        "action_policy": "report_only",
        "mod_queue_threshold": 0.5,
        "review_channel": 42,
        "ban_purge_hours": 24,
        "safe_mode": False,
        "retention_days": 30,
        "locale": "en",
        "optin_global_db": False,
        "optin_scan_bots": False,
        "optin_evidence_storage": False,
    }
    assert set(full_config) == set(_CONFIG_VIEW_ORDER)
    resp = await handle_command(_ctx("config", subcommand="view"), FakeDeps(config=full_config))
    summary = resp.params["summary"]
    assert summary.count("-# ") == len(_CONFIG_VIEW_ORDER)
    assert "Default: report_only" in summary  # spot-check one explanation


@pytest.mark.asyncio
async def test_config_view_explanations_follow_guild_locale() -> None:
    deps = FakeDeps(config={"locale": "sr"})
    resp = await handle_command(_ctx("config", subcommand="view"), deps)
    assert "Podrazumevano" in resp.params["summary"]  # Serbian "Default"


@pytest.mark.asyncio
async def test_confirm_scam_button_runs_the_moderation_pipeline() -> None:
    """Confirm must enforce the guild's action policy, not just delete one message.

    The reported incident: a moderator confirmed a scam and the bot removed
    that single message without banning the uploader, so Discord's ban-time
    cross-channel purge never ran and copies in every other channel survived.
    Routing the confirmation through the verdict pipeline is what makes the
    configured delete+ban policy (and the campaign sweep behind it) apply.
    """
    deps = FakeDeps()
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=5)

    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)

    assert resp.i18n_key == "button.confirmed_scam"
    assert len(deps.confirmed_scams) == 1
    submitted = deps.confirmed_scams[0]
    assert submitted["matched_hash_id"] == f"{0xABC:016x}"
    # The uploader and message are carried through, so enforcement targets the
    # scammer and the right post (see the fabricated DetectionFacts defaults).
    assert submitted["uploader_id"] == 333
    assert submitted["channel_id"] == 111
    assert submitted["message_id"] == 222


@pytest.mark.asyncio
async def test_reviewmsg_reply_warns_when_permissions_already_rule_the_action_out() -> None:
    """The reply must not promise enforcement the bot cannot perform.

    This is the reported incident's user-visible half: a moderator submitted a
    scam, read a confident acknowledgement, and nothing happened. The gap is
    knowable at reply time, so it is stated instead of promised.
    """
    deps = FakeDeps(
        config={"action_policy": "delete_ban", "review_channel": 444},
        enforcement_blocked="Could not remove the message in <#111>: missing View Channel.",
    )
    resp = await handle_command(_review_ctx(attachments=[(1, "https://x/1.png")]), deps)

    assert resp.i18n_key == "command.reviewmsg_result_blocked"
    message = render(resp, "en")
    assert "View Channel" in message
    # The check is scoped to the channel the reported message lives in.
    assert deps.blocked_checks == [(1, 111, "delete_ban")]
    # The hashes and verdict are still recorded: the campaign is learned even
    # when this guild cannot act on it, so other servers stay protected.
    assert len(deps.confirmed_scams) == 1


@pytest.mark.asyncio
async def test_reviewmsg_permission_check_is_skipped_for_non_acting_policies() -> None:
    """Nothing will be enforced under report_only, so there is nothing to check.

    Probing anyway would put a permission warning on a server that deliberately
    only reports.
    """
    deps = FakeDeps(
        config={"action_policy": "report_only"},
        enforcement_blocked="should never be consulted",
    )
    resp = await handle_command(_review_ctx(attachments=[(1, "https://x/1.png")]), deps)

    assert resp.i18n_key == "command.reviewmsg_result_report_only"
    assert deps.blocked_checks == []


@pytest.mark.asyncio
async def test_reviewmsg_permission_check_is_skipped_in_safe_mode() -> None:
    deps = FakeDeps(
        config={"action_policy": "delete_ban", "safe_mode": True},
        enforcement_blocked="should never be consulted",
    )
    resp = await handle_command(_review_ctx(attachments=[(1, "https://x/1.png")]), deps)

    assert resp.i18n_key == "command.reviewmsg_result_safe_mode"
    assert deps.blocked_checks == []


@pytest.mark.asyncio
async def test_reviewmsg_reply_stays_optimistic_when_nothing_is_in_the_way() -> None:
    """The check must not add noise to the ordinary, working case."""
    deps = FakeDeps(config={"action_policy": "delete_ban", "review_channel": 444})
    resp = await handle_command(_review_ctx(attachments=[(1, "https://x/1.png")]), deps)

    assert resp.i18n_key == "command.reviewmsg_result_submitted"
    assert deps.blocked_checks == [(1, 111, "delete_ban")]
