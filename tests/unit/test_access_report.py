"""``/config permissions``: name what actually needs a moderator's attention.

The report has to be honest in both directions. It never calls a guild healthy
when something is broken -- above all the review channel, without which no card
reaches a moderator -- and it never invents a problem: not in a channel the
server ignores, not in a private channel hidden from the bot on purpose, and
not a missing Manage Messages while the server only reports.
"""

from __future__ import annotations

import re
from typing import Any

from optimus.contracts.events import Action
from optimus.i18n import available_locales, translate
from optimus.shared.explain import explain_access_report, explain_rescan_summary
from optimus.shared.permissions import (
    ADMINISTRATOR,
    EMBED_LINKS,
    MANAGE_MESSAGES,
    SEND_MESSAGES,
    VIEW_CHANNEL,
    AccessReport,
    build_access_report,
    punitive_requirement,
)

_FULL = VIEW_CHANNEL | MANAGE_MESSAGES | SEND_MESSAGES | EMBED_LINKS
#: Sees and posts, cannot delete: a public channel on a report-only server.
_SEES = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS
#: Discord's Ban Members bit, which delete_ban's punitive step needs.
_BAN = 1 << 2
REVIEW = 500


def _report(channels: list[tuple[int, int]], **kw: Any) -> AccessReport:
    """A report for ``channels`` plus a working review channel."""
    kw.setdefault("review_channel_id", REVIEW)
    return build_access_report([*channels, (REVIEW, _SEES)], **kw)


# -- the review channel -----------------------------------------------------------


def test_a_review_channel_the_bot_cannot_see_is_the_headline() -> None:
    """Tonight's outage: every card 403'd and the old report never mentioned it."""
    report = build_access_report([(10, _FULL), (REVIEW, 0)], review_channel_id=REVIEW)

    assert not report.ok
    assert report.review_missing == ("View Channel", "Send Messages", "Embed Links")
    text = explain_access_report(report, "en")
    assert text.startswith(f"**Review cards can't be posted to <#{REVIEW}>.**")
    assert "reach no moderator" in text


def test_review_channel_needs_embed_links_too() -> None:
    report = build_access_report([(REVIEW, VIEW_CHANNEL | SEND_MESSAGES)], review_channel_id=REVIEW)
    assert report.review_missing == ("Embed Links",)
    assert not report.ok


def test_a_hidden_review_channel_is_not_counted_as_private() -> None:
    """The one private channel the bot must reach is never filed under 'hidden'."""
    report = build_access_report([(REVIEW, 0)], review_channel_id=REVIEW)
    assert report.hidden == 0
    assert report.checked == 0


def test_a_deleted_review_channel_is_reported() -> None:
    report = build_access_report([(10, _FULL)], review_channel_id=REVIEW)
    assert report.review_channel_missing
    assert not report.ok
    assert "no longer exists" in explain_access_report(report, "en")


def test_no_review_channel_is_not_healthy() -> None:
    report = build_access_report([(10, _FULL)])
    assert not report.ok
    assert "No review channel is set" in explain_access_report(report, "en")


def test_working_review_channel_is_confirmed() -> None:
    text = explain_access_report(_report([(10, _FULL)]), "en")
    assert text.splitlines()[0] == f"Review cards post to <#{REVIEW}>."


# -- private channels -------------------------------------------------------------


def test_channels_hidden_from_the_bot_are_counted_not_flagged() -> None:
    """Staff, beta and archive channels hide on purpose; that is not a fault."""
    report = _report([(10, 0), (11, 0), (12, _FULL)])

    assert report.ok
    assert report.hidden == 2
    assert report.checked == 1
    text = explain_access_report(report, "en")
    assert "<#10>" not in text
    assert "<#11>" not in text
    assert "2 channel(s) are hidden from the bot" in text


def test_administrator_is_never_hidden() -> None:
    report = _report([(10, ADMINISTRATOR)])
    assert report.hidden == 0
    assert report.checked == 1


def test_unknown_permissions_fail_open_rather_than_hide() -> None:
    """A channel whose permissions are not cached is checked, not dismissed."""
    report = build_access_report(
        [(10, None), (REVIEW, _SEES)],  # type: ignore[list-item]
        review_channel_id=REVIEW,
    )
    assert report.hidden == 0
    assert report.checked == 1
    assert report.ok


def test_ignored_channels_are_counted_but_never_flagged() -> None:
    report = _report([(10, _SEES), (11, _FULL)], ignored_channels=frozenset({10}))

    assert report.ok
    assert report.checked == 1
    assert report.ignored == 1
    text = explain_access_report(report, "en")
    assert "1 channel(s) are on this server's ignore list" in text
    assert "/config" not in text  # there is no command behind it yet


# -- the action policy decides what counts ------------------------------------------


def test_report_only_does_not_call_missing_manage_messages_a_fault() -> None:
    """Visible channels are watched and reported in; deleting is not switched on."""
    report = _report([(10, _SEES), (11, _SEES), (12, _FULL)], deletes=False)

    assert report.ok
    assert report.blocked == ()
    assert report.advisory == (
        (10, ("Manage Messages",)),
        (11, ("Manage Messages",)),
    )
    text = explain_access_report(report, "en")
    assert "Before you switch on deleting" in text
    assert "<#10>" in text
    assert "can't delete" not in text
    assert "The bot is watching all 3 channels it can see." in text


def test_a_deleting_policy_flags_channels_it_cannot_delete_in() -> None:
    report = _report([(10, _SEES), (12, _FULL)], deletes=True)

    assert not report.ok
    assert report.blocked == ((10, ("Manage Messages",)),)
    assert report.advisory == ()
    text = explain_access_report(report, "en")
    assert "can't delete in 1 of the 2 channels it watches" in text
    assert "still reported, just not removed" in text
    assert "<#12>" not in text  # a working channel is not listed


def test_guild_wide_punitive_gap_is_reported_once() -> None:
    report = _report(
        [(10, _FULL)],
        guild_permissions=_FULL,
        punitive=punitive_requirement(Action.DELETE_BAN),
    )

    assert not report.ok
    assert report.blocked == ()
    assert report.guild_missing == ("Ban Members",)


def test_unknown_guild_permissions_do_not_invent_a_punitive_gap() -> None:
    """Silence must mean \"unknown\", never \"blocked\" -- same rule as preflight."""
    report = _report(
        [(10, _FULL)], guild_permissions=None, punitive=punitive_requirement(Action.DELETE_BAN)
    )
    assert report.ok


def test_a_long_list_is_capped_with_a_count() -> None:
    """Twenty blocked channels must not produce an unreadable wall of mentions."""
    report = _report([(cid, _SEES) for cid in range(100, 120)], deletes=True)

    text = explain_access_report(report, "en")

    assert "and 5 more" in text
    assert text.count("<#") == 15 + 1  # plus the review channel line


def test_the_reported_server_reads_as_one_real_problem() -> None:
    """The shape from the bug report: 91 channels, most private, report-only.

    The old report said the bot "cannot act in 75 of 91 channels" and listed
    staff and beta rooms. The only real fault was the review channel.
    """
    channels = (
        [(1000 + i, 0) for i in range(63)]
        + [(2000 + i, _SEES) for i in range(12)]
        + [(3000 + i, _FULL) for i in range(15)]
        + [(REVIEW, 0)]
    )
    report = build_access_report(channels, deletes=False, review_channel_id=REVIEW)
    text = explain_access_report(report, "en")

    assert report.hidden == 63
    assert report.blocked == ()
    assert len(report.advisory) == 12
    assert not report.ok  # only because of the review channel
    assert "cannot act in" not in text
    assert "<#1000>" not in text
    assert len(text) < 2000  # fits one Discord message


# -- rendering in every locale ---------------------------------------------------------


def test_rescan_summary_names_the_channels_and_the_work_done() -> None:
    text = explain_rescan_summary((10, 11), 42, "en")

    assert "<#10>" in text
    assert "<#11>" in text
    assert "42" in text


def test_every_locale_renders_without_leftover_placeholders() -> None:
    reports = [
        build_access_report(
            [(10, 0), (11, _SEES), (12, _FULL), (REVIEW, 0)],
            ignored_channels=frozenset({99}),
            guild_permissions=_FULL,
            punitive=punitive_requirement(Action.DELETE_BAN),
            deletes=deletes,
            review_channel_id=REVIEW,
        )
        for deletes in (True, False)
    ]
    reports.append(build_access_report([(10, _FULL)]))  # no review channel
    reports.append(build_access_report([(10, _FULL)], review_channel_id=REVIEW))  # deleted
    reports.append(build_access_report([(10, _FULL), (REVIEW, _SEES)], review_channel_id=REVIEW))
    locales = list(available_locales())
    assert "sr" in locales  # guard against silently testing English only

    for locale in locales:
        for text in [explain_access_report(r, locale) for r in reports] + [
            explain_rescan_summary((10,), 3, locale)
        ]:
            assert not re.search(r"\{[a-z_]+\}", text), (locale, text)
            # A key missing from the catalog renders as the key itself, so a
            # surviving "command." prefix means an untranslated string shipped.
            assert "command." not in text, (locale, text)


def test_the_new_keys_are_translated_in_every_locale() -> None:
    """Keys without a parameter, so a missing one is visible as the key itself."""
    for locale in available_locales():
        for key in ("permissions_unknown", "permissions_how_to_fix", "permissions_review_unset"):
            assert translate(f"command.{key}", locale) != f"command.{key}", (locale, key)
