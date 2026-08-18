"""Interaction handlers: server-side auth, audit, button auth, appeal lifecycle."""

from __future__ import annotations

from typing import Any

import pytest

from optimus.db.models import GuildHash, GuildWhitelist
from optimus.globaldb.service import GlobalHashService, SubmissionDenied
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
        self.opted_out: list[int] = []
        self.safe_mode_disabled: list[int] = []
        self.config_set: list[tuple[str, Any]] = []
        self.resolved: list[tuple[int, bool]] = []
        self._hash_rate_ok = flags.get("hash_rate_ok", True)
        self._report_rate_ok = flags.get("report_rate_ok", True)
        self.user_reports: list[dict[str, Any]] = []
        self._appeal_ok = flags.get("appeal_ok", True)
        self._recent_detection = flags.get("recent_detection", 555)
        self._owned_detections: set[int] = set(flags.get("owned_detections", {77}))
        self._next_appeal_id = 1
        self._global_service = _FakeGlobalService(flags.get("submit_error"))
        self.global_submitted: list[str] = []
        #: attachment_id -> exception to raise, or a hash_id string to return.
        self._attachment_outcomes: dict[int, Any] = flags.get("attachment_outcomes", {})
        self.confirmed_scams: list[dict[str, Any]] = []
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

    async def opt_out_user(self, user_id: int) -> int:
        self.opted_out.append(user_id)
        return 1

    async def purge_guild(self, guild_id: int) -> int:
        self.purged.append(guild_id)
        return 7

    async def recent_detection_for(self, guild_id: int, user_id: int) -> int | None:
        return self._recent_detection

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

    def global_service(self) -> GlobalHashService:
        return self._global_service  # type: ignore[return-value]

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
        uploader_id: int,
        reporter_id: int,
    ) -> None:
        self.user_reports.append(
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "message_id": message_id,
                "attachment_id": attachment_id,
                "uploader_id": uploader_id,
                "reporter_id": reporter_id,
            }
        )


class _FakeGlobalService:
    """Minimal stand-in for :class:`GlobalHashService` used by submit_global."""

    def __init__(self, error: str | None) -> None:
        self._error = error
        self.submitted: list[str] = []

    async def submit(self, *, hash_id: str, **_: Any) -> None:
        if self._error is not None:
            raise SubmissionDenied(self._error)
        self.submitted.append(hash_id)


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


@pytest.mark.asyncio
async def test_forget_me_allowed_in_dm_without_permission() -> None:
    deps = FakeDeps()
    resp = await handle_command(_ctx("forget_me", perms=NONE, guild_id=None), deps)
    assert resp.i18n_key == "command.forget_me_ok"
    assert deps.opted_out == [99]


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
    assert resp.params["value"] == "<#1402357722430570498>"


@pytest.mark.asyncio
async def test_config_set_review_channel_clear_renders_as_none() -> None:
    deps = FakeDeps()
    resp = await handle_command(
        _ctx("config", subcommand="set", field="review_channel", value="none"), deps
    )
    assert resp.i18n_key == "command.config_set_ok"
    assert deps.config_set == [("review_channel", None)]
    assert resp.params["value"] == "none"


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
            "reason": "Optimus: scam image (detection #5)",
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
async def test_submit_global_button_requires_confirmed_local_hash() -> None:
    """Without a blocklisted local hash the button points at Confirm scam."""
    deps = FakeDeps()  # detection has hashes, but nothing in the local blocklist
    parsed = ParsedCustomId(action=ReviewAction.SUBMIT_GLOBAL, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.confirm_first"
    assert not deps._global_service.submitted


@pytest.mark.asyncio
async def test_submit_global_button_submits_confirmed_hash() -> None:
    deps = FakeDeps()
    # Simulate a prior Confirm scam press having blocklisted the image.
    confirm = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=5)
    await handle_review_button(_ctx("", perms=MANAGE), confirm, deps)
    parsed = ParsedCustomId(action=ReviewAction.SUBMIT_GLOBAL, detection_id=5)
    resp = await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert resp.i18n_key == "button.submitted_global"
    assert deps._global_service.submitted == [f"{0xABC:016x}"]
    assert deps.audits[-1][2] == "review.submit_global"


@pytest.mark.asyncio
async def test_submit_global_button_rate_limited_rejected() -> None:
    deps = FakeDeps(submit_error="rate_limited")
    confirm = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=5)
    await handle_review_button(_ctx("", perms=MANAGE), confirm, deps)
    parsed = ParsedCustomId(action=ReviewAction.SUBMIT_GLOBAL, detection_id=5)
    with pytest.raises(InteractionRejected) as exc:
        await handle_review_button(_ctx("", perms=MANAGE), parsed, deps)
    assert exc.value.reason is CommandError.RATE_LIMITED


# --- appeal lifecycle ----------------------------------------------------------


@pytest.mark.asyncio
async def test_appeal_open_via_command() -> None:
    deps = FakeDeps()
    resp = await handle_command(_ctx("appeal", perms=NONE), deps)
    assert resp.i18n_key == "command.appeal_opened"
    assert deps.appeals
    assert deps.audits[0][2] == "appeal.open"


@pytest.mark.asyncio
async def test_appeal_command_cooldown() -> None:
    deps = FakeDeps(appeal_ok=False)
    resp = await handle_command(_ctx("appeal", perms=NONE), deps)
    assert resp.i18n_key == "dm.appeal_cooldown"
    assert not deps.appeals


@pytest.mark.asyncio
async def test_appeal_command_no_detection() -> None:
    deps = FakeDeps(recent_detection=None)
    resp = await handle_command(_ctx("appeal", perms=NONE), deps)
    assert resp.i18n_key == "command.appeal_none"


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
    assert "**review_channel**: `<#1402357722430570498>`" in view_resp.params["summary"]
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


# --- submit_global -------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_global_ok() -> None:
    deps = FakeDeps()
    deps.hashes["abc"] = GuildHash(
        hash_id="abc", phash=1, dhash=2, whash=3, ahash=0, source="local"
    )
    resp = await handle_command(_ctx("submit_global", hash_id="abc"), deps)
    assert resp.i18n_key == "command.submit_global_ok"
    assert deps._global_service.submitted == ["abc"]
    assert deps.audits[0][2] == "global.submit"


@pytest.mark.asyncio
async def test_submit_global_unknown_hash() -> None:
    deps = FakeDeps()
    resp = await handle_command(_ctx("submit_global", hash_id="nope"), deps)
    assert resp.i18n_key == "command.hash_not_found"


@pytest.mark.asyncio
async def test_submit_global_below_threshold_rejected() -> None:
    deps = FakeDeps(submit_error="below_threshold")
    deps.hashes["abc"] = GuildHash(
        hash_id="abc", phash=1, dhash=2, whash=3, ahash=0, source="local"
    )
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_ctx("submit_global", hash_id="abc"), deps)
    assert exc.value.reason is CommandError.BELOW_THRESHOLD


@pytest.mark.asyncio
async def test_submit_global_rate_limited_rejected() -> None:
    deps = FakeDeps(submit_error="rate_limited")
    deps.hashes["abc"] = GuildHash(
        hash_id="abc", phash=1, dhash=2, whash=3, ahash=0, source="local"
    )
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_ctx("submit_global", hash_id="abc"), deps)
    assert exc.value.reason is CommandError.RATE_LIMITED


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
