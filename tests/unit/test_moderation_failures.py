"""Failure classification: the layer that decides what a moderator is told.

Every assertion here defends one of three decisions the rest of the pipeline
takes from a classified failure: whether the step actually succeeded anyway,
whether retrying could ever help, and which localized instruction to print. The
incident that motivated the module collapsed all of them into one opaque
``ForbiddenError``, so the specificity itself is the behaviour under test.
"""

from __future__ import annotations

import pytest

from optimus.core.circuit import CircuitOpenError
from optimus.services.moderation.failures import Failure, FailureKind, classify


class _DiscordError(Exception):
    """Duck-typed stand-in for a hikari HTTP error."""

    def __init__(self, *, status: int = 0, code: int = 0) -> None:
        super().__init__("discord")
        self.status = status
        self.code = code


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (10003, FailureKind.UNKNOWN_CHANNEL),
        (10007, FailureKind.MEMBER_GONE),
        (10008, FailureKind.ALREADY_GONE),
        (10013, FailureKind.MEMBER_GONE),
        (10026, FailureKind.ALREADY_GONE),
        (30035, FailureKind.BAN_LIMIT),
        (40007, FailureKind.ALREADY_BANNED),
        (50001, FailureKind.MISSING_ACCESS),
        (50007, FailureKind.DM_BLOCKED),
        (50013, FailureKind.MISSING_PERMISSION),
        (50021, FailureKind.SYSTEM_MESSAGE),
        (50024, FailureKind.UNSUPPORTED_TARGET),
        (60003, FailureKind.MFA_REQUIRED),
    ],
)
def test_json_error_code_drives_classification(code: int, expected: FailureKind) -> None:
    assert classify(_DiscordError(status=403, code=code)).kind is expected


def test_json_code_wins_over_http_status() -> None:
    """50001 and 50013 are both HTTP 403 but need different fixes.

    Reporting the status alone is what made "the bot cannot see the channel"
    indistinguishable from "the bot cannot delete here" -- the whole reason the
    code is preferred.
    """
    no_access = classify(_DiscordError(status=403, code=50001))
    no_permission = classify(_DiscordError(status=403, code=50013))
    assert no_access.kind is FailureKind.MISSING_ACCESS
    assert no_permission.kind is FailureKind.MISSING_PERMISSION
    assert no_access.message_key != no_permission.message_key


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, FailureKind.MISSING_PERMISSION),
        (404, FailureKind.ALREADY_GONE),
        (429, FailureKind.RATE_LIMITED),
        (500, FailureKind.TRANSIENT),
        (503, FailureKind.TRANSIENT),
    ],
)
def test_http_status_is_the_fallback(status: int, expected: FailureKind) -> None:
    assert classify(_DiscordError(status=status)).kind is expected


def test_unmapped_json_code_falls_back_to_status() -> None:
    """An unknown code must not defeat classification of a known status."""
    assert classify(_DiscordError(status=429, code=123456)).kind is FailureKind.RATE_LIMITED


def test_real_hikari_errors_classify_by_status() -> None:
    """Guards the assumption the status fallback rests on.

    hikari raises typed exceptions rather than exposing Discord's JSON code, so
    if a version stopped populating ``status`` every 403 and 404 would silently
    degrade to UNKNOWN -- reported as a bug instead of a permission fix.
    """
    import hikari

    not_found = hikari.NotFoundError(url="u", headers={}, raw_body=b"")
    forbidden = hikari.ForbiddenError(url="u", headers={}, raw_body=b"")
    assert classify(not_found).kind is FailureKind.ALREADY_GONE
    assert classify(not_found).satisfied is True
    assert classify(forbidden).kind is FailureKind.MISSING_PERMISSION


def test_circuit_open_is_classified_before_any_http_inspection() -> None:
    failure = classify(CircuitOpenError("open"))
    assert failure.kind is FailureKind.CIRCUIT_OPEN
    assert failure.recoverable is True


def test_unrecognized_exception_is_unknown_and_keeps_its_name() -> None:
    """An unclassifiable error must stay visible, not be silently absorbed."""
    failure = classify(RuntimeError("boom"))
    assert failure.kind is FailureKind.UNKNOWN
    assert failure.satisfied is False
    assert failure.detail == "unknown:RuntimeError"


@pytest.mark.parametrize("kind", [FailureKind.ALREADY_GONE, FailureKind.ALREADY_BANNED])
def test_satisfied_kinds_mean_the_goal_is_already_true(kind: FailureKind) -> None:
    """These must count as success or a re-run would loop forever on them."""
    failure = Failure(kind)
    assert failure.satisfied is True
    assert failure.recoverable is False


@pytest.mark.parametrize(
    "kind",
    [
        FailureKind.MISSING_ACCESS,
        FailureKind.MISSING_PERMISSION,
        FailureKind.MFA_REQUIRED,
        FailureKind.RATE_LIMITED,
        FailureKind.CIRCUIT_OPEN,
        FailureKind.TRANSIENT,
    ],
)
def test_recoverable_kinds_could_succeed_later(kind: FailureKind) -> None:
    assert Failure(kind).recoverable is True


@pytest.mark.parametrize(
    "kind",
    [
        FailureKind.ALREADY_GONE,
        FailureKind.ALREADY_BANNED,
        FailureKind.DM_BLOCKED,
        FailureKind.SYSTEM_MESSAGE,
        FailureKind.UNSUPPORTED_TARGET,
        FailureKind.UNKNOWN_CHANNEL,
        FailureKind.MEMBER_GONE,
        FailureKind.BAN_LIMIT,
        FailureKind.DUPLICATE,
        FailureKind.UNKNOWN,
    ],
)
def test_non_recoverable_kinds_must_not_invite_a_retry(kind: FailureKind) -> None:
    """Retrying any of these can never change the outcome.

    This is the guard against the resource waste the whole change is about: a
    channel the bot cannot see must not be hammered once per scam image.
    """
    assert Failure(kind).recoverable is False


@pytest.mark.parametrize(
    "kind",
    [FailureKind.MISSING_ACCESS, FailureKind.MISSING_PERMISSION, FailureKind.MFA_REQUIRED],
)
def test_permission_related_kinds_are_admin_fixable(kind: FailureKind) -> None:
    assert Failure(kind).permission_related is True


@pytest.mark.parametrize(
    "kind", [FailureKind.RATE_LIMITED, FailureKind.TRANSIENT, FailureKind.UNKNOWN]
)
def test_non_permission_kinds_do_not_blame_permissions(kind: FailureKind) -> None:
    """Telling an admin to fix permissions for a Discord outage wastes their time."""
    assert Failure(kind).permission_related is False


def test_detail_includes_the_discord_code_when_known() -> None:
    assert classify(_DiscordError(status=403, code=50001)).detail == "missing_access:50001"


def test_detail_is_the_bare_kind_without_a_code() -> None:
    assert Failure(FailureKind.MISSING_ACCESS).detail == "missing_access"


def test_message_key_matches_the_kind_value() -> None:
    """The i18n catalog is keyed off this, so drift here silently loses wording."""
    assert Failure(FailureKind.MISSING_ACCESS).message_key == "failure.missing_access"


def test_every_kind_has_a_catalog_entry() -> None:
    """A kind with no message would print a raw key to a moderator."""
    from optimus.i18n import translate

    for kind in FailureKind:
        key = Failure(kind).message_key
        rendered = translate(key, "en", channel="", missing="")
        assert rendered != key, f"missing en catalog entry for {key}"
