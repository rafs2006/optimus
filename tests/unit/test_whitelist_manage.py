"""Whitelist management: see it, undo a misclick, and let a scam call win.

A misclicked False positive (or Whitelist image) used to be invisible and
permanent: the whitelist wins over the blocklist, so a real scam image stayed
exempt even after a moderator listed it. Now:

* False positive and Whitelist image name the entries they create on the card;
* Confirm scam, Review as scam and ``/scamhash add`` lift the entries that
  cover the image they call a scam;
* ``/scamhash whitelist`` lists entries; ``/scamhash unwhitelist`` removes them
  by number or hash, or in a batch by moderator and time (preview, then
  ``confirm:True``);
* export carries the whitelist for people to read; import ignores it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from optimus.contracts.events import VerdictEvent
from optimus.db.models import GuildHash, GuildWhitelist
from optimus.i18n import translate
from optimus.services.interactions.commands import COMMANDS
from optimus.services.interactions.handlers import (
    InteractionContext,
    handle_command,
)
from optimus.services.interactions.logic import (
    CommandError,
    InteractionRejected,
    validate_import,
)
from optimus.services.interactions.review_buttons import handle_review_button
from optimus.shared.review import (
    ParsedCustomId,
    ReportData,
    ReviewAction,
    decided_note,
    merge_reports,
)
from tests.unit.test_interactions_handlers import MANAGE, MOD, FakeDeps, _review_ctx

MOD_A = 7001
MOD_B = 7002
NOW = datetime.now(UTC)


def _cmd(sub: str, **options: Any) -> InteractionContext:
    return InteractionContext(
        guild_id=1,
        user_id=99,
        member_permissions=MANAGE,
        command="scamhash",
        subcommand=sub,
        options=options,
    )


def _text(resp: Any, locale: str = "en") -> str:
    return translate(resp.i18n_key, locale, **resp.params)


async def _wl(
    deps: FakeDeps,
    phash: int,
    *,
    by: int = MOD_A,
    ago: timedelta = timedelta(hours=1),
    reason: str | None = "false positive: detection #463",
) -> GuildWhitelist:
    return await deps.add_whitelist(
        1,
        GuildWhitelist(
            phash=phash, dhash=0, whash=0, reason=reason, added_by=by, created_at=NOW - ago
        ),
    )


def _far(phash: int) -> int:
    return ~phash & (2**64 - 1)


# -- cards name the entries they create ---------------------------------------


async def test_false_positive_card_names_the_new_entry() -> None:
    deps = FakeDeps()
    parsed = ParsedCustomId(action=ReviewAction.FALSE_POSITIVE, detection_id=5)
    ctx = InteractionContext(guild_id=1, user_id=99, member_permissions=MOD, command="", options={})
    resp = await handle_review_button(ctx, parsed, deps)
    note = translate(resp.card_note_key, "en", **resp.card_note_params)
    assert note.endswith("Whitelisted 1 image(s): #1.")
    assert "#1" in translate(resp.card_note_key, "sr", **resp.card_note_params)


async def test_whitelist_image_card_names_the_new_entry() -> None:
    deps = FakeDeps()
    await _wl(deps, _far(0xABC))  # an older, unrelated entry: the new one is #2
    parsed = ParsedCustomId(action=ReviewAction.WHITELIST_IMAGE, detection_id=5)
    ctx = InteractionContext(guild_id=1, user_id=99, member_permissions=MOD, command="", options={})
    resp = await handle_review_button(ctx, parsed, deps)
    assert resp.card_note_key == "card.handled_whitelisted"
    assert resp.card_note_params["entries"] == "#2"


# -- a scam call lifts the whitelist ------------------------------------------


async def test_confirm_scam_lifts_covering_entries_and_tells_the_card() -> None:
    deps = FakeDeps()
    near = await _wl(deps, 0xABC ^ 0b11)  # 2 bits away: the scanner's radius covers it
    far = await _wl(deps, _far(0xABC))
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=9)
    ctx = InteractionContext(guild_id=1, user_id=99, member_permissions=MOD, command="", options={})
    await handle_review_button(ctx, parsed, deps)
    assert [w.id for w in deps.whitelisted] == [far.id]
    assert deps.whitelist_removed_sent == [1]
    assert ("scamhash.unwhitelist", f"#{near.id} (Confirm scam on detection #9)") in [
        (a[2], a[3]) for a in deps.audits
    ]


async def test_confirm_scam_with_nothing_whitelisted_sends_zero() -> None:
    deps = FakeDeps()
    parsed = ParsedCustomId(action=ReviewAction.CONFIRM_SCAM, detection_id=9)
    ctx = InteractionContext(guild_id=1, user_id=99, member_permissions=MOD, command="", options={})
    await handle_review_button(ctx, parsed, deps)
    assert deps.whitelist_removed_sent == [0]
    assert not [a for a in deps.audits if a[2] == "scamhash.unwhitelist"]


async def test_review_as_scam_lifts_covering_entries() -> None:
    deps = FakeDeps(attachment_outcomes={1: f"{0x5A5A:016x}"})
    await _wl(deps, 0x5A5A)
    await handle_command(_review_ctx(attachments=[(1, "https://x/1.png")]), deps)
    assert deps.whitelisted == []
    assert deps.whitelist_removed_sent == [1]


def _report(**kw: Any) -> ReportData:
    base: dict[str, Any] = {
        "detection_id": 1,
        "guild_id": 1,
        "channel_id": 2,
        "message_id": 3,
        "uploader_id": 4,
        "verdict": "scam",
        "confidence": 1.0,
        "action_taken": "deleted",
        "decided_by": 99,
    }
    return ReportData(**{**base, **kw})


def test_the_folded_confirm_card_says_entries_were_lifted() -> None:
    merged = merge_reports([_report(whitelist_removed=2), _report(detection_id=2)])
    assert merged.whitelist_removed == 2
    assert decided_note(merged).splitlines()[-1] == (
        "Removed 2 whitelist entr(y/ies) that covered this image."
    )
    assert "whitelist" not in decided_note(_report())


def test_the_verdict_event_defaults_to_nothing_lifted() -> None:
    assert VerdictEvent.model_fields["whitelist_removed"].default == 0


# -- /scamhash whitelist ------------------------------------------------------


async def test_an_empty_whitelist_says_so() -> None:
    resp = await handle_command(_cmd("whitelist"), FakeDeps())
    assert _text(resp) == "This server's whitelist is empty."


async def test_the_list_shows_number_reason_who_when_and_overridden_hashes() -> None:
    deps = FakeDeps()
    deps.hashes[f"{0xC478:016x}"] = GuildHash(
        hash_id=f"{0xC478:016x}", phash=0xC478, dhash=0, whash=0, ahash=0, source="local"
    )
    entry = await _wl(deps, 0xC478)
    await _wl(deps, _far(0xC478), reason="review: detection #12", ago=timedelta(days=2))
    resp = await handle_command(_cmd("whitelist"), deps)
    text = _text(resp)
    lines = text.splitlines()
    assert lines[0] == "This server's whitelist has 2 entr(y/ies). Page 1/1, newest first:"
    stamp = int((NOW - timedelta(hours=1)).timestamp())
    assert lines[1] == (
        f"\u2022 **#{entry.id}** \u2014 **False positive** on detection #463 "
        f"by <@{MOD_A}> <t:{stamp}:d>"
    )
    assert lines[2] == f"  Overrides blocked hash(es): `{0xC478:016x}`"
    assert lines[3].startswith("\u2022 **#2** \u2014 **Whitelist image** on detection #12")
    assert lines[-1] == "Remove entries with `/scamhash unwhitelist entry:<number>`."
    assert "#2" in _text(resp, "sr")


async def test_the_list_pages_by_ten_and_clamps_the_page() -> None:
    deps = FakeDeps()
    for i in range(12):
        await _wl(deps, 1 << (i * 5), ago=timedelta(minutes=i + 1))
    first = await handle_command(_cmd("whitelist"), deps)
    assert first.params["pages"] == 2
    assert first.params["entries"].count("\u2022") == 10
    last = await handle_command(_cmd("whitelist", page=9), deps)
    assert last.params["page"] == 2
    assert last.params["entries"].count("\u2022") == 2


async def test_the_list_filters_by_moderator_and_time() -> None:
    deps = FakeDeps()
    await _wl(deps, 1, by=MOD_A, ago=timedelta(minutes=10))
    await _wl(deps, 1 << 20, by=MOD_B, ago=timedelta(minutes=10))
    await _wl(deps, 1 << 40, by=MOD_A, ago=timedelta(days=3))
    resp = await handle_command(_cmd("whitelist", by=MOD_A, since="2h"), deps)
    assert resp.params["count"] == 1
    assert _text(resp).splitlines()[0] == (
        f"This server's whitelist has 1 entr(y/ies) added by <@{MOD_A}> in the last 2h. "
        "Page 1/1, newest first:"
    )
    none = await handle_command(_cmd("whitelist", by=MOD_B, since="1m"), deps)
    assert _text(none) == f"No whitelist entries added by <@{MOD_B}> in the last 1m."


@pytest.mark.parametrize("since", ["2", "h", "0h", "-1h", "2y", "53w", "abc"])
async def test_a_bad_since_is_refused(since: str) -> None:
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_cmd("whitelist", since=since), FakeDeps())
    assert exc.value.reason is CommandError.BAD_SINCE


# -- /scamhash unwhitelist ----------------------------------------------------


async def test_remove_by_numbers_and_hash_reports_what_was_missing() -> None:
    deps = FakeDeps()
    a = await _wl(deps, 0x1111)
    b = await _wl(deps, 0x2222 << 30)
    keep = await _wl(deps, _far(0x1111))
    resp = await handle_command(
        _cmd("unwhitelist", entry=f"#{a.id}, {0x2222 << 30:016x} 77 nope"), deps
    )
    assert [w.id for w in deps.whitelisted] == [keep.id]
    assert _text(resp) == (
        f"Removed 2 whitelist entr(y/ies): #{a.id}, #{b.id}. Optimus flags those images again."
        "\nNot found: #77, nope."
    )
    assert [a_[3] for a_ in deps.audits if a_[2] == "scamhash.unwhitelist"] == [
        f"#{a.id}",
        f"#{b.id}",
    ]


async def test_remove_with_nothing_matching_changes_nothing() -> None:
    deps = FakeDeps()
    await _wl(deps, 0x1111)
    resp = await handle_command(_cmd("unwhitelist", entry="42"), deps)
    assert _text(resp) == "No whitelist entry matches #42."
    assert len(deps.whitelisted) == 1


async def test_entry_and_filter_together_are_refused() -> None:
    resp = await handle_command(_cmd("unwhitelist", entry="1", by=MOD_A), FakeDeps())
    assert resp.i18n_key == "command.unwhitelist_entry_or_filter"


async def test_no_input_explains_the_options() -> None:
    resp = await handle_command(_cmd("unwhitelist"), FakeDeps())
    assert resp.i18n_key == "command.unwhitelist_nothing_given"


async def test_a_batch_is_previewed_first_and_removed_only_with_confirm() -> None:
    deps = FakeDeps()
    for i in range(3):
        await _wl(deps, 1 << (i * 20), by=MOD_A, ago=timedelta(minutes=5))
    other = await _wl(deps, _far(1), by=MOD_B, ago=timedelta(minutes=5))
    old = await _wl(deps, 1 << 63, by=MOD_A, ago=timedelta(days=2))

    preview = await handle_command(_cmd("unwhitelist", by=MOD_A, since="1h"), deps)
    assert len(deps.whitelisted) == 5  # nothing removed yet
    assert _text(preview) == (
        f"3 whitelist entr(y/ies) added by <@{MOD_A}> in the last 1h: #3, #2, #1.\n"
        "Nothing is removed yet. Run the same command with `confirm:True` to remove them."
    )

    done = await handle_command(_cmd("unwhitelist", by=MOD_A, since="1h", confirm=True), deps)
    assert done.i18n_key == "command.unwhitelist_done"
    assert done.params["count"] == 3
    assert sorted(w.id for w in deps.whitelisted) == sorted([other.id, old.id])


async def test_a_big_batch_preview_is_shortened() -> None:
    deps = FakeDeps()
    for i in range(25):
        await _wl(deps, i + 1, ago=timedelta(minutes=1))
    preview = await handle_command(_cmd("unwhitelist", since="1h"), deps)
    assert preview.params["entries"].endswith(" +5 more")
    assert preview.params["count"] == 25


async def test_a_batch_with_no_match_says_so() -> None:
    deps = FakeDeps()
    await _wl(deps, 1, by=MOD_A)
    resp = await handle_command(_cmd("unwhitelist", by=MOD_B, confirm=True), deps)
    assert resp.i18n_key == "command.whitelist_none_match"
    assert len(deps.whitelisted) == 1


# -- export / import ----------------------------------------------------------


async def test_export_carries_the_whitelist_and_import_ignores_it() -> None:
    deps = FakeDeps()
    deps.hashes[f"{0xAB:016x}"] = GuildHash(
        hash_id=f"{0xAB:016x}", phash=0xAB, dhash=1, whash=2, ahash=0, source="local"
    )
    entry = await _wl(deps, 0xC478)
    resp = await handle_command(_cmd("export"), deps)
    assert resp.params == {"count": 1, "whitelisted": 1}
    doc = json.loads(resp.attachment)
    assert doc["whitelist"][0]["entry"] == entry.id
    assert doc["whitelist"][0]["phash"] == f"{0xC478:016x}"
    imported = validate_import(resp.attachment)
    assert [e.phash for e in imported] == [0xAB]


async def test_export_with_only_a_whitelist_still_exports() -> None:
    deps = FakeDeps()
    await _wl(deps, 0xC478)
    resp = await handle_command(_cmd("export"), deps)
    assert resp.i18n_key == "command.export_ok"
    assert json.loads(resp.attachment)["hashes"] == []


# -- registration ---------------------------------------------------------------


def test_both_subcommands_are_registered_with_short_descriptions() -> None:
    scamhash = next(c for c in COMMANDS if c.name == "scamhash")
    subs = {s.name: s for s in scamhash.subcommands}
    assert [o.name for o in subs["whitelist"].options] == ["page", "by", "since"]
    assert [o.name for o in subs["unwhitelist"].options] == ["entry", "by", "since", "confirm"]
    for sub in (subs["whitelist"], subs["unwhitelist"]):
        assert len(sub.description) <= 100
        assert all(len(o.description) <= 100 for o in sub.options)


async def test_an_old_entry_without_reason_author_or_date_still_lists() -> None:
    deps = FakeDeps()
    await deps.add_whitelist(1, GuildWhitelist(phash=1, dhash=0, whash=0))
    resp = await handle_command(_cmd("whitelist"), deps)
    assert resp.params["entries"] == "\u2022 **#1** \u2014 no reason recorded"
    # Without a date it can never fall inside a since: window.
    none = await handle_command(_cmd("whitelist", since="1w"), deps)
    assert none.i18n_key == "command.whitelist_none_match"


async def test_an_entry_the_bot_added_is_credited_to_optimus() -> None:
    deps = FakeDeps()
    await _wl(deps, 1, by=0)
    resp = await handle_command(_cmd("whitelist"), deps)
    assert " by Optimus <t:" in resp.params["entries"]
    assert "<@0>" not in resp.params["entries"]
