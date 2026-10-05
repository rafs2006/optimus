"""Mod-review channel: custom_id scheme, report content, and provisioning.

The interactive button ``custom_id`` scheme is ``om:v1:<action>:<detection_id>``.
Encoding/decoding and the report's textual content are kept pure so they are
unit-testable; the hikari embed/action-row construction and the channel
provisioning REST calls live behind thin adapters at the bottom of the module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
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
    DISMISS = "dismiss"


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
    #: Verdict confidence 0..1 for cards from the live path. ``None`` on
    #: replayed cards from the ``/setup`` backlog: the score is a runtime
    #: property of the verdict, and the persisted detection row does not
    #: store it. Replayed cards show the rest of the evidence and rely on a
    #: click-through jump link for the image.
    confidence: float | None
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
    #: How many flagged images from the message this card covers. Images from
    #: one message share one card (see :func:`merge_reports`).
    image_count: int = 1
    #: Further still-live images from the same message, shown alongside
    #: ``image_url`` as a gallery (Discord renders up to four per message).
    extra_image_urls: tuple[str, ...] = ()
    #: Set when a moderator already confirmed this (Confirm scam, "Review as
    #: scam"): the card is rendered folded -- outcome only, no buttons.
    decided_by: int | None = None
    #: Later posts by the confirmed uploader removed without a card of their
    #: own; counted on the confirmed card.
    followups_removed: int = 0
    #: Whitelist entries the confirmation lifted; shown on the folded card.
    whitelist_removed: int = 0
    #: Set when the bot fully handled this on its own (a match against this
    #: server's blocklist, the configured action applied in full): the card is
    #: folded like a confirmed one but keeps a False positive button.
    auto_handled: bool = False


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
    if data.image_count > 1:
        return translate(
            "report.title_group",
            data.locale,
            detection_id=data.detection_id,
            verdict=data.verdict.upper(),
            count=data.image_count,
        )
    return translate(
        "report.title", data.locale, detection_id=data.detection_id, verdict=data.verdict.upper()
    )


#: Discord shows at most four images in one message's embed gallery.
MAX_CARD_IMAGES = 4
#: Discord's embed field value limit.
_FIELD_LIMIT = 1024
#: Verdict strength, strongest last: the merged card shows the strongest one.
_VERDICT_RANK = {"clean": 0, "ambiguous": 1, "scam": 2}


def _unique(values: Sequence[str | None]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


#: Separates an action from the notes appended to it (boundary, sweep, cleanup).
_ACTION_NOTE_SEP = " — "


def _merge_actions(values: Sequence[str | None]) -> str:
    """One ``Action taken`` line for a card holding several images.

    Each image's outcome is ``<action>[ — note]...``, and the campaign notes
    (purged N more, cleared N reports) land on only one of them, so plain
    de-duplication printed ``delete_ban; delete_ban — purged ...``. Distinct
    actions are listed once, joined by ``; ``, and distinct notes follow once.
    """
    heads: list[str] = []
    notes: list[str] = []
    for value in values:
        if not value:
            continue
        head, *rest = value.split(_ACTION_NOTE_SEP)
        if head not in heads:
            heads.append(head)
        for note in rest:
            if note not in notes:
                notes.append(note)
    return _ACTION_NOTE_SEP.join(["; ".join(heads), *notes]) if heads else ""


def _clip(text: str) -> str:
    return text if len(text) <= _FIELD_LIMIT else text[: _FIELD_LIMIT - 1] + "\u2026"


def merge_reports(items: Sequence[ReportData]) -> ReportData:
    """Fold the reports for every flagged image of one message into one card.

    The first item (lowest detection id) gives the card its number and its
    buttons; the rest contribute evidence. Each field keeps the most useful
    value across images: the strongest verdict and highest confidence, every
    distinct matched hash, action, OCR finding and problem, and every live
    image for the gallery. A single item comes back unchanged.
    """
    if not items:
        raise ValueError("merge_reports needs at least one report")
    first = items[0]
    if len(items) == 1:
        return first
    verdict = max((i.verdict for i in items), key=lambda v: _VERDICT_RANK.get(v, 0))
    confidences = [i.confidence for i in items if i.confidence is not None]
    images = _unique([i.image_url for i in items])
    swarm = [i.swarm_guilds for i in items if i.swarm_guilds]
    return replace(
        first,
        verdict=verdict,
        confidence=max(confidences) if confidences else None,
        action_taken=_clip(_merge_actions([i.action_taken for i in items])),
        matched_hash_id=", ".join(_unique([i.matched_hash_id for i in items])) or None,
        global_match=any(i.global_match for i in items),
        swarm_guilds=max(swarm) if swarm else None,
        evidence_url=next((i.evidence_url for i in items if i.evidence_url), None),
        image_url=images[0] if images else None,
        extra_image_urls=tuple(images[1:MAX_CARD_IMAGES]),
        reported_by=next((i.reported_by for i in items if i.reported_by), None),
        ocr_summary=_clip("\n".join(_unique([i.ocr_summary for i in items]))) or None,
        problem=_clip("\n".join(_unique([i.problem for i in items]))) or None,
        partial=any(i.partial for i in items),
        image_count=len(items),
        decided_by=next((i.decided_by for i in items if i.decided_by), None),
        followups_removed=max(i.followups_removed for i in items),
        whitelist_removed=max(i.whitelist_removed for i in items),
        auto_handled=any(i.auto_handled for i in items),
    )


def report_fields(data: ReportData) -> list[tuple[str, str]]:
    """The ordered (localized name, value) field pairs for the report embed."""
    loc = data.locale
    fields: list[tuple[str, str]] = [
        (translate("report.field_uploader", loc), f"<@{data.uploader_id}>"),
        (translate("report.field_channel", loc), f"<#{data.channel_id}>"),
        (translate("report.field_message", loc), message_reference(data)),
        (translate("report.field_action", loc), data.action_taken),
    ]
    if data.confidence is not None:
        # Insert directly after Message so live cards render exactly as before;
        # replayed cards (no confidence) just omit the row rather than showing
        # a placeholder that would look like a real 0.00 score.
        fields.insert(3, (translate("report.field_confidence", loc), f"{data.confidence:.2f}"))
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
    ReviewAction.DISMISS,
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
    ReviewAction.DISMISS: "Dismiss",
}


def build_embed(data: ReportData) -> object:
    """Build a hikari embed for ``data`` (imported lazily to keep this testable)."""
    return build_embeds(data)[0]


def build_embeds(data: ReportData) -> list[Any]:
    """The card's embeds: the report, plus one per extra image for the gallery.

    Discord merges embeds that share a ``url`` into one embed with an image
    grid, which is how one card shows up to four images. The shared url is
    the scanned message's jump link, so the title doubles as a link to it.
    """
    import hikari

    url = jump_url(data.guild_id, data.channel_id, data.message_id)
    embed = hikari.Embed(title=report_title(data), url=url)
    for name, value in report_fields(data):
        embed.add_field(name=name, value=value, inline=True)
    if data.image_url:
        embed.set_image(data.image_url)
    embeds = [embed]
    for extra in data.extra_image_urls:
        embeds.append(hikari.Embed(url=url).set_image(extra))
    return embeds


#: Embed colour of a folded (decided) card: Discord's muted grey.
FOLDED_COLOUR = 0x4F545C


def folded_text(title: str | None, note: str) -> str:
    """The one-line body of a decided card: its title, then who did what."""
    return f"**{title}**\n{note}" if title else note


def build_folded_embed(title: str | None, note: str, url: str | None = None) -> Any:
    """The small grey embed a card collapses to once a moderator decides."""
    import hikari

    return hikari.Embed(description=folded_text(title, note), url=url, colour=FOLDED_COLOUR)


#: A card the bot settled by itself has no buttons: nothing on it can be
#: misclicked. Its text names the ``/queue detection:`` command that posts it
#: again as a full card, with Unban and False positive.
AUTO_HANDLED_BUTTONS: tuple[ReviewAction, ...] = ()

#: Application command ids by name, filled in once commands are registered,
#: so a card can mention ``/queue`` as a clickable command (``</queue:id>``).
#: Empty until then (or in tests): the card falls back to plain ``/queue``.
COMMAND_IDS: dict[str, int] = {}


def command_mention(name: str) -> str:
    """``</name:id>`` when the command id is known, else plain ``/name``."""
    command_id = COMMAND_IDS.get(name)
    return f"</{name}:{command_id}>" if command_id else f"/{name}"


def build_action_rows(
    detection_id: int, buttons: Sequence[ReviewAction] | None = None
) -> list[object]:
    """Build hikari message action rows with the review buttons.

    ``buttons`` defaults to :data:`REVIEW_BUTTONS`, read at call time.
    """
    import hikari

    if buttons is None:
        buttons = REVIEW_BUTTONS

    rows: list[object] = []
    row = hikari.impl.MessageActionRowBuilder()
    buttons_in_row = 0
    for action in buttons:
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


def decided_note(data: ReportData) -> str:
    """The folded body of a decided card: who (a moderator or the bot), and the outcome."""
    loc = data.locale
    who = (
        translate("card.handled_auto", loc)
        if data.decided_by is None and data.auto_handled
        else translate(
            "card.handled",
            loc,
            action=BUTTON_LABELS[ReviewAction.CONFIRM_SCAM],
            user_id=data.decided_by,
        )
    )
    lines = [
        who,
        f"{translate('report.field_action', loc)}: {data.action_taken}",
    ]
    if data.decided_by is None and data.auto_handled:
        # The post is gone; its ID (and the uploader) stay as evidence.
        lines.append(
            translate(
                "card.original_message",
                loc,
                url=jump_url(data.guild_id, data.channel_id, data.message_id),
                message_id=data.message_id,
                user_id=data.uploader_id,
            )
        )
        lines.append(
            translate(
                "card.review_hint",
                loc,
                command=command_mention("queue"),
                detection_id=data.detection_id,
            )
        )
    if data.problem:
        lines.append(data.problem)
    if data.whitelist_removed:
        lines.append(translate("card.whitelist_removed", loc, count=data.whitelist_removed))
    if data.followups_removed:
        lines.append(translate("card.followups_removed", loc, count=data.followups_removed))
    return "\n".join(lines)


def build_card(items: Sequence[ReportData]) -> tuple[list[Any], list[object]]:
    """Embeds and button rows for the card covering ``items`` (one message).

    An undecided card is the full report with buttons. A card a moderator
    already confirmed is folded: title, who confirmed it and what enforcement
    did, no buttons -- there is nothing left to decide on it.
    """
    merged = merge_reports(items)
    if merged.decided_by is not None:
        url = jump_url(merged.guild_id, merged.channel_id, merged.message_id)
        return [build_folded_embed(report_title(merged), decided_note(merged), url)], []
    if merged.auto_handled:
        # Settled by the bot itself: folded the same way, keeping only the
        # one decision left -- undoing a wrong call.
        url = jump_url(merged.guild_id, merged.channel_id, merged.message_id)
        embeds = [build_folded_embed(report_title(merged), decided_note(merged), url)]
        return embeds, build_action_rows(merged.detection_id, AUTO_HANDLED_BUTTONS)
    return build_embeds(merged), build_action_rows(merged.detection_id)
