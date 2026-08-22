"""``/config permissions``: name the channels the bot cannot act in.

The point of these is that the report is honest in both directions -- it never
calls a guild healthy when something is blocked, and it never invents a problem
in a channel the server told it to ignore.
"""

from __future__ import annotations

import re

from optimus.contracts.events import Action
from optimus.i18n import available_locales, translate
from optimus.services.moderation.explain import explain_access_report, explain_rescan_summary
from optimus.services.moderation.permissions import (
    MANAGE_MESSAGES,
    VIEW_CHANNEL,
    build_access_report,
    punitive_requirement,
)

_FULL = VIEW_CHANNEL | MANAGE_MESSAGES
#: Discord's Ban Members bit, which delete_ban's punitive step needs.
_BAN = 1 << 2


def test_clean_guild_reports_no_blocked_channels() -> None:
    report = build_access_report([(10, _FULL), (11, _FULL)])

    assert report.ok
    assert report.checked == 2
    assert report.blocked == ()


def test_a_denied_channel_is_named_with_what_it_needs() -> None:
    report = build_access_report([(10, _FULL), (11, 0)])

    assert not report.ok
    assert report.blocked == ((11, ("View Channel", "Manage Messages")),)
    assert report.checked == 2


def test_ignored_channels_are_counted_but_never_flagged() -> None:
    """A channel excluded by /config is not a permission problem."""
    report = build_access_report([(10, 0), (11, _FULL)], ignored_channels=frozenset({10}))

    assert report.ok
    assert report.checked == 1
    assert report.ignored == 1


def test_guild_wide_punitive_gap_is_reported_once() -> None:
    report = build_access_report(
        [(10, _FULL)],
        guild_permissions=_FULL,
        punitive=punitive_requirement(Action.DELETE_BAN),
    )

    assert not report.ok
    assert report.blocked == ()
    assert report.guild_missing == ("Ban Members",)


def test_unknown_guild_permissions_do_not_invent_a_punitive_gap() -> None:
    """Silence must mean \"unknown\", never \"blocked\" -- same rule as preflight."""
    report = build_access_report(
        [(10, _FULL)], guild_permissions=None, punitive=punitive_requirement(Action.DELETE_BAN)
    )

    assert report.ok


def test_channels_missing_the_same_permission_group_together() -> None:
    report = build_access_report([(10, VIEW_CHANNEL), (11, VIEW_CHANNEL), (12, 0)])

    grouped = report.grouped()

    # Largest bucket first, so the biggest single fix is what a mod reads first.
    assert grouped[0] == (("Manage Messages",), (10, 11))
    assert grouped[1] == (("View Channel", "Manage Messages"), (12,))


# -- rendering ----------------------------------------------------------------


def test_healthy_guild_renders_a_single_reassuring_line() -> None:
    text = explain_access_report(build_access_report([(10, _FULL)]), "en")

    assert text == "Enforcement is working in all 1 channels."


def test_blocked_channels_render_as_mentions_with_a_fix() -> None:
    report = build_access_report([(10, _FULL), (11, 0)])

    text = explain_access_report(report, "en")

    assert "cannot act in 1 of 2 channels" in text
    assert "<#11>" in text
    assert "<#10>" not in text  # a working channel is not listed
    assert "Edit Channel" in text


def test_a_long_list_is_capped_with_a_count() -> None:
    """Twenty blocked channels must not produce an unreadable wall of mentions."""
    report = build_access_report([(cid, 0) for cid in range(100, 120)])

    text = explain_access_report(report, "en")

    assert "and 5 more" in text
    assert text.count("<#") == 15


def test_ignored_channels_are_disclosed_not_hidden() -> None:
    report = build_access_report([(10, 0), (11, _FULL)], ignored_channels=frozenset({11}))

    text = explain_access_report(report, "en")

    assert "1 channel(s) excluded" in text


def test_rescan_summary_names_the_channels_and_the_work_done() -> None:
    text = explain_rescan_summary((10, 11), 42, "en")

    assert "<#10>" in text
    assert "<#11>" in text
    assert "42" in text


def test_every_locale_renders_without_leftover_placeholders() -> None:
    report = build_access_report(
        [(10, 0), (11, VIEW_CHANNEL), (12, _FULL)],
        ignored_channels=frozenset({99}),
        guild_permissions=_FULL,
        punitive=punitive_requirement(Action.DELETE_BAN),
    )
    locales = list(available_locales())
    assert "sr" in locales  # guard against silently testing English only

    for locale in locales:
        for text in (
            explain_access_report(report, locale),
            explain_access_report(build_access_report([(10, _FULL)]), locale),
            explain_rescan_summary((10,), 3, locale),
        ):
            assert not re.search(r"\{[a-z_]+\}", text), (locale, text)
            # A key missing from the catalog renders as the key itself, so a
            # surviving "command." prefix means an untranslated string shipped.
            assert "command." not in text, (locale, text)


def test_the_new_keys_are_translated_in_every_locale() -> None:
    """Keys without a parameter, so a missing one is visible as the key itself."""
    for locale in available_locales():
        for key in ("permissions_unknown", "permissions_how_to_fix"):
            assert translate(f"command.{key}", locale) != f"command.{key}", (locale, key)
