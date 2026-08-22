"""Unit tests for the review custom_id scheme and report content."""

from __future__ import annotations

import pytest

from optimus.services.moderation import review as review_mod
from optimus.services.moderation.review import (
    BUTTON_LABELS,
    REVIEW_BUTTONS,
    ReportData,
    ReviewAction,
    build_action_rows,
    build_embed,
    decode_custom_id,
    encode_custom_id,
    report_fields,
    report_title,
)


@pytest.mark.parametrize("action", list(ReviewAction))
def test_custom_id_roundtrips(action: ReviewAction) -> None:
    cid = encode_custom_id(action, 12345)
    assert cid.startswith("om:v1:")
    parsed = decode_custom_id(cid)
    assert parsed is not None
    assert parsed.action is action
    assert parsed.detection_id == 12345


def test_decode_rejects_foreign_custom_id() -> None:
    assert decode_custom_id("other:thing:confirm_scam:1") is None
    assert decode_custom_id("om:v1:confirm_scam") is None
    assert decode_custom_id("om:v1:not_an_action:1") is None
    assert decode_custom_id("om:v1:confirm_scam:notint") is None


def test_all_buttons_are_offered_except_legacy_submit_global() -> None:
    # SUBMIT_GLOBAL stays in the enum so clicks on old cards still decode,
    # but new cards no longer offer it: Confirm scam casts the global vote.
    assert set(REVIEW_BUTTONS) == set(ReviewAction) - {ReviewAction.SUBMIT_GLOBAL}
    assert ReviewAction.SUBMIT_GLOBAL in BUTTON_LABELS


def test_build_action_rows_default_layout() -> None:
    rows = build_action_rows(42)
    sizes = [len(row.components) for row in rows]  # type: ignore[attr-defined]
    assert sum(sizes) == len(REVIEW_BUTTONS)
    assert all(0 < n <= 5 for n in sizes)


@pytest.mark.parametrize("count", [1, 4, 5, 6, 10, 11])
def test_build_action_rows_never_emits_empty_or_overfull_row(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    # A button count that is a multiple of 5 previously produced a trailing
    # empty action row, which Discord rejects.
    buttons = tuple((list(ReviewAction) * 3)[:count])
    monkeypatch.setattr(review_mod, "REVIEW_BUTTONS", buttons)
    rows = build_action_rows(1)
    sizes = [len(row.components) for row in rows]  # type: ignore[attr-defined]
    assert sum(sizes) == count
    assert all(0 < n <= 5 for n in sizes)


def test_report_title_and_fields() -> None:
    data = ReportData(
        detection_id=9,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=42,
        verdict="scam",
        confidence=0.91,
        action_taken="delete_ban",
        matched_hash_id="camp-7",
        swarm_guilds=4,
        evidence_url="https://example/x",
    )
    assert "#9" in report_title(data)
    field_names = [name for name, _ in report_fields(data)]
    assert "Uploader" in field_names
    assert "Matched hash" in field_names
    assert "Swarm" in field_names
    assert "Evidence" in field_names


def test_report_fields_omit_optional_when_absent() -> None:
    data = ReportData(
        detection_id=1,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=4,
        verdict="ambiguous",
        confidence=0.6,
        action_taken="report_only",
    )
    names = [name for name, _ in report_fields(data)]
    assert "Matched hash" not in names
    assert "Swarm" not in names
    assert "Evidence" not in names


def test_build_embed_renders_title_and_one_field_per_report_field() -> None:
    import hikari

    data = ReportData(
        detection_id=9,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=42,
        verdict="scam",
        confidence=0.91,
        action_taken="delete_ban",
        matched_hash_id="camp-7",
    )
    embed = build_embed(data)
    assert isinstance(embed, hikari.Embed)
    assert embed.title is not None and "#9" in embed.title
    expected = report_fields(data)
    assert len(embed.fields) == len(expected)
    rendered = {(f.name, f.value) for f in embed.fields}
    assert rendered == set(expected)
    assert all(f.is_inline for f in embed.fields)


def test_report_fields_show_reporter_for_member_reports() -> None:
    data = ReportData(
        detection_id=12,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=4,
        verdict="ambiguous",
        confidence=1.0,
        action_taken="report_only",
        reported_by=777,
    )
    fields = dict(report_fields(data))
    assert fields["Reported by"] == "<@777>"
    # And absent for automated detections.
    data_auto = ReportData(
        detection_id=13,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=4,
        verdict="scam",
        confidence=0.9,
        action_taken="delete_ban",
    )
    assert "Reported by" not in dict(report_fields(data_auto))


def test_report_fields_flag_global_matches() -> None:
    data = ReportData(
        detection_id=9,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=4,
        verdict="scam",
        confidence=0.95,
        action_taken="report_only",
        global_match=True,
    )
    fields = dict(report_fields(data))
    assert "never auto-acted" in fields["Source"]

    data_local = ReportData(
        detection_id=9,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=4,
        verdict="scam",
        confidence=0.95,
        action_taken="delete_ban",
    )
    assert "Source" not in dict(report_fields(data_local))


def test_report_names_the_problem_when_enforcement_was_refused() -> None:
    """A moderator must learn the fix from the card, not from the logs.

    The reported incident produced clean-looking cards while the message stayed
    up; the problem field is what makes a permission gap visible where the
    person who can fix it is already looking.
    """
    data = ReportData(
        detection_id=9,
        guild_id=1,
        channel_id=1402887429324673035,
        message_id=3,
        uploader_id=42,
        verdict="scam",
        confidence=0.91,
        action_taken="delete_ban",
        partial=True,
        problem="Could not remove the message in <#1402887429324673035>: missing View Channel.",
    )
    fields = dict(report_fields(data))
    assert "Needs attention" in fields
    assert "View Channel" in fields["Needs attention"]
    # The partial marker is separate so the card reads correctly at a glance:
    # the user was banned, the message was not removed.
    assert "Partially applied" in fields


def test_report_omits_both_markers_on_a_clean_enforcement() -> None:
    data = ReportData(
        detection_id=9,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=42,
        verdict="scam",
        confidence=0.91,
        action_taken="delete_ban",
    )
    names = [name for name, _ in report_fields(data)]
    assert "Needs attention" not in names
    assert "Partially applied" not in names


# --- The card has to be verifiable: a jump link and the image itself ----------


def _card(**overrides: object) -> ReportData:
    base: dict[str, object] = {
        "detection_id": 9,
        "guild_id": 1402357722430570498,
        "channel_id": 1402887429324673035,
        "message_id": 1517941473855799567,
        "uploader_id": 1409938900482396400,
        "verdict": "ambiguous",
        "confidence": 1.0,
        "action_taken": "report_only",
    }
    base.update(overrides)
    return ReportData(**base)  # type: ignore[arg-type]


def test_the_message_field_is_a_jump_link_that_still_shows_the_id() -> None:
    """Verifying a report used to mean hunting for the message by hand."""
    data = _card()
    value = dict(report_fields(data))["Message"]

    assert value == (
        "[1517941473855799567](https://discord.com/channels/"
        "1402357722430570498/1402887429324673035/1517941473855799567)"
    )
    assert str(data.message_id) in value  # still copy-pasteable into /scamhash


def test_the_image_is_rendered_on_the_card_when_it_still_exists() -> None:
    import hikari

    embed = build_embed(_card(image_url="https://cdn.example/scam.png?ex=1"))

    assert isinstance(embed, hikari.Embed)
    assert embed.image is not None
    assert embed.image.url == "https://cdn.example/scam.png?ex=1"


def test_no_image_is_set_when_there_is_nothing_to_show() -> None:
    """An empty image slot beats a broken one on a deleted attachment."""
    import hikari

    embed = build_embed(_card())

    assert isinstance(embed, hikari.Embed)
    assert embed.image is None


def test_the_image_does_not_displace_any_field() -> None:
    with_image = build_embed(_card(image_url="https://cdn.example/scam.png"))
    without = build_embed(_card())

    assert len(with_image.fields) == len(without.fields)  # type: ignore[attr-defined]
