"""Mod-review channel: custom_id scheme, report content, and provisioning.

The interactive button ``custom_id`` scheme is ``om:v1:<action>:<detection_id>``.
Encoding/decoding and the report's textual content are kept pure so they are
unit-testable; the hikari embed/action-row construction and the channel
provisioning REST calls live behind thin adapters at the bottom of the module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from optimus.i18n import translate

CUSTOM_ID_PREFIX = "om:v1"


class ReviewAction(StrEnum):
    """The moderator actions offered as buttons on a report."""

    CONFIRM_SCAM = "confirm_scam"
    FALSE_POSITIVE = "false_positive"
    BAN_UPLOADER = "ban_uploader"
    UNBAN = "unban"
    WHITELIST_IMAGE = "whitelist_image"
    SUBMIT_GLOBAL = "submit_global"


def encode_custom_id(action: ReviewAction, detection_id: int) -> str:
    """Build the ``om:v1:<action>:<detection_id>`` component custom id."""
    return f"{CUSTOM_ID_PREFIX}:{action.value}:{detection_id}"


@dataclass(frozen=True, slots=True)
class ParsedCustomId:
    """A decoded review button interaction id."""

    action: ReviewAction
    detection_id: int


def decode_custom_id(custom_id: str) -> ParsedCustomId | None:
    """Parse a review ``custom_id``; return ``None`` if it is not one of ours."""
    parts = custom_id.split(":")
    if len(parts) != 4 or f"{parts[0]}:{parts[1]}" != CUSTOM_ID_PREFIX:
        return None
    try:
        action = ReviewAction(parts[2])
        detection_id = int(parts[3])
    except (ValueError, KeyError):
        return None
    return ParsedCustomId(action=action, detection_id=detection_id)


@dataclass(frozen=True, slots=True)
class ReportData:
    """The facts rendered into a moderator report embed."""

    detection_id: int
    guild_id: int
    channel_id: int
    message_id: int
    uploader_id: int
    verdict: str
    confidence: float
    action_taken: str
    matched_hash_id: str | None = None
    #: True when the match came from the shared global scam set (other
    #: communities' confirmations). Rendered as an explicit call-to-action:
    #: the bot never auto-acts on these — a local Confirm is required.
    global_match: bool = False
    swarm_guilds: int | None = None
    evidence_url: str | None = None
    #: The still-live image, rendered inline on the card so a moderator can judge
    #: what they are approving without leaving the review channel. Set only when
    #: the message was *not* deleted -- a deleted attachment's CDN URL 404s, and
    #: a broken image on the card is worse than none. Stored evidence
    #: (``evidence_url``) is the path for images that are already gone.
    image_url: str | None = None
    #: Set when a member filed this via "Report scam to mods" -- shown on the
    #: card so moderators know it is a human report, not an automated match.
    reported_by: int | None = None
    #: Pre-rendered OCR/QR risk-scan evidence (risk level, signals, lookalike
    #: domains, QR payloads) when that lane drove the verdict.
    ocr_summary: str | None = None
    #: Why enforcement could not be completed, phrased as an instruction (e.g.
    #: "grant View Channel in #general"). Rendered prominently so a permission
    #: gap is never mistaken for a bot bug.
    problem: str | None = None
    #: True when the offender was punished but some step still failed, so the
    #: card must not read as a clean success.
    partial: bool = False
    locale: str = "en"


def jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    """The canonical Discord deep link to one message."""
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def message_reference(data: ReportData) -> str:
    """The message field's value: a clickable jump link that still shows the id.

    The raw id is kept visible because moderators paste it into
    ``/scamhash review``; the link is what makes the card actionable, since
    verifying a report previously meant hunting for the message by hand.
    """
    url = jump_url(data.guild_id, data.channel_id, data.message_id)
    return f"[{data.message_id}]({url})"


def report_title(data: ReportData) -> str:
    """A short, localized title for the report."""
    return translate(
        "report.title", data.locale, detection_id=data.detection_id, verdict=data.verdict.upper()
    )


def report_fields(data: ReportData) -> list[tuple[str, str]]:
    """The ordered (localized name, value) field pairs for the report embed."""
    loc = data.locale
    fields: list[tuple[str, str]] = [
        (translate("report.field_uploader", loc), f"<@{data.uploader_id}>"),
        (translate("report.field_channel", loc), f"<#{data.channel_id}>"),
        (translate("report.field_message", loc), message_reference(data)),
        (translate("report.field_confidence", loc), f"{data.confidence:.2f}"),
        (translate("report.field_action", loc), data.action_taken),
    ]
    if data.matched_hash_id:
        fields.append((translate("report.field_matched_hash", loc), data.matched_hash_id))
    if data.global_match:
        fields.append(
            (translate("report.field_global", loc), translate("report.field_global_value", loc))
        )
    if data.reported_by:
        fields.append((translate("report.field_reported_by", loc), f"<@{data.reported_by}>"))
    if data.ocr_summary:
        fields.append((translate("report.field_ocr", loc), data.ocr_summary))
    if data.partial:
        fields.append(
            (translate("report.field_partial", loc), translate("report.field_partial_value", loc))
        )
    if data.problem:
        fields.append((translate("report.field_problem", loc), data.problem))
    if data.swarm_guilds:
        fields.append(
            (
                translate("report.field_swarm", loc),
                translate("report.field_swarm_value", loc, count=data.swarm_guilds),
            )
        )
    if data.evidence_url:
        fields.append((translate("report.field_evidence", loc), data.evidence_url))
    return fields


#: The buttons shown on a report, in display order. ``SUBMIT_GLOBAL`` is
#: deliberately absent: global contribution is automatic — a Confirm on an
#: approved, opted-in server *is* the global vote. The action enum member is
#: kept so clicks on old cards still parse (and get a friendly explanation).
REVIEW_BUTTONS: tuple[ReviewAction, ...] = (
    ReviewAction.CONFIRM_SCAM,
    ReviewAction.FALSE_POSITIVE,
    ReviewAction.BAN_UPLOADER,
    ReviewAction.UNBAN,
    ReviewAction.WHITELIST_IMAGE,
)

BUTTON_LABELS: dict[ReviewAction, str] = {
    ReviewAction.CONFIRM_SCAM: "Confirm scam",
    ReviewAction.FALSE_POSITIVE: "False positive",
    ReviewAction.BAN_UPLOADER: "Ban uploader",
    ReviewAction.UNBAN: "Unban",
    ReviewAction.WHITELIST_IMAGE: "Whitelist image",
    ReviewAction.SUBMIT_GLOBAL: "Submit to global",
}


def build_embed(data: ReportData) -> object:
    """Build a hikari embed for ``data`` (imported lazily to keep this testable)."""
    import hikari

    embed = hikari.Embed(title=report_title(data))
    for name, value in report_fields(data):
        embed.add_field(name=name, value=value, inline=True)
    if data.image_url:
        embed.set_image(data.image_url)
    return embed


def build_action_rows(detection_id: int) -> list[object]:
    """Build hikari message action rows with the review buttons."""
    import hikari

    rows: list[object] = []
    row = hikari.impl.MessageActionRowBuilder()
    buttons_in_row = 0
    for action in REVIEW_BUTTONS:
        style = (
            hikari.ButtonStyle.SUCCESS
            if action is ReviewAction.CONFIRM_SCAM
            else hikari.ButtonStyle.DANGER
            if action in (ReviewAction.BAN_UPLOADER, ReviewAction.FALSE_POSITIVE)
            else hikari.ButtonStyle.SECONDARY
        )
        # Discord allows up to 5 buttons per row; start a fresh row when full.
        if buttons_in_row == 5:
            rows.append(row)
            row = hikari.impl.MessageActionRowBuilder()
            buttons_in_row = 0
        row.add_interactive_button(
            cast("Any", style),
            encode_custom_id(action, detection_id),
            label=BUTTON_LABELS[action],
        )
        buttons_in_row += 1
    if buttons_in_row:
        rows.append(row)
    return rows
