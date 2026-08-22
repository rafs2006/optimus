"""Rendering failures as instructions, in both catalog locales.

The point of these assertions is that a moderator reading the review card learns
*what to change*. A message that names no permission and no channel is the
failure mode this module exists to remove, so the tests check for the concrete
nouns rather than merely that some string came back.
"""

from __future__ import annotations

import pytest

from optimus.contracts.events import Action
from optimus.services.moderation.actions import ActionResult, Step, StepOutcome
from optimus.services.moderation.explain import explain_preflight, explain_result, explain_step
from optimus.services.moderation.failures import Failure, FailureKind
from optimus.services.moderation.permissions import PreflightResult

_CHANNEL = 1402887429324673035


def _outcome(
    step: Step,
    kind: FailureKind,
    *,
    missing: tuple[str, ...] = (),
) -> StepOutcome:
    return StepOutcome(step=step, success=False, failure=Failure(kind), missing=missing)


def test_step_failure_names_the_step_and_the_fix() -> None:
    rendered = explain_step(
        _outcome(Step.DELETE, FailureKind.MISSING_ACCESS), "en", channel_id=_CHANNEL
    )
    assert "Could not remove the message" in rendered
    assert f"<#{_CHANNEL}>" in rendered
    assert "View Channel" in rendered


def test_named_variant_quotes_the_permissions_the_preflight_found() -> None:
    rendered = explain_step(
        _outcome(Step.BAN, FailureKind.MISSING_PERMISSION, missing=("Ban Members",)),
        "en",
    )
    assert "Could not ban the user" in rendered
    assert "Ban Members" in rendered


def test_missing_permissions_on_a_kind_without_a_named_variant_still_reads_cleanly() -> None:
    """A raw catalog key must never reach a moderator.

    Only two kinds have a ``_named`` message; asking for a variant that does not
    exist would print ``failure.transient_named`` verbatim.
    """
    rendered = explain_step(
        _outcome(Step.BAN, FailureKind.TRANSIENT, missing=("Ban Members",)), "en"
    )
    assert "failure." not in rendered


def test_successful_result_has_nothing_to_explain() -> None:
    result = ActionResult(Action.DELETE_BAN, success=True, detail="ok")
    assert explain_result(result, "en") is None


def test_partial_result_explains_only_the_failed_step() -> None:
    """The ban landed, the delete did not -- and the card must say which."""
    result = ActionResult(
        Action.DELETE_BAN,
        success=True,
        detail="partial",
        steps=(
            _outcome(Step.DELETE, FailureKind.MISSING_ACCESS),
            StepOutcome(step=Step.BAN, success=True),
        ),
    )
    rendered = explain_result(result, "en", channel_id=_CHANNEL)
    assert rendered is not None
    assert "Could not remove the message" in rendered
    assert "Could not ban the user" not in rendered


def test_each_failed_step_gets_its_own_line() -> None:
    result = ActionResult(
        Action.DELETE_BAN,
        success=False,
        detail="missing_permission",
        steps=(
            _outcome(Step.DELETE, FailureKind.MISSING_ACCESS),
            _outcome(Step.BAN, FailureKind.MISSING_PERMISSION, missing=("Ban Members",)),
        ),
    )
    rendered = explain_result(result, "en", channel_id=_CHANNEL)
    assert rendered is not None
    assert len(rendered.splitlines()) == 2


def test_channel_mention_is_only_used_for_the_channel_scoped_step() -> None:
    """Naming a channel next to a ban would point an admin at the wrong page.

    Ban, kick and timeout are guild-level permissions.
    """
    result = ActionResult(
        Action.DELETE_BAN,
        success=False,
        detail="missing_permission",
        steps=(
            _outcome(Step.DELETE, FailureKind.MISSING_ACCESS),
            _outcome(Step.BAN, FailureKind.MISSING_PERMISSION),
        ),
    )
    rendered = explain_result(result, "en", channel_id=_CHANNEL)
    assert rendered is not None
    delete_line, ban_line = rendered.splitlines()
    assert f"<#{_CHANNEL}>" in delete_line
    assert f"<#{_CHANNEL}>" not in ban_line


def test_preflight_refusal_renders_before_any_request_is_made() -> None:
    result = PreflightResult(
        ok=False,
        missing=("View Channel",),
        failure=Failure(FailureKind.MISSING_ACCESS),
    )
    rendered = explain_preflight(result, "en", channel_id=_CHANNEL)
    assert rendered is not None
    assert "View Channel" in rendered
    assert f"<#{_CHANNEL}>" in rendered


def test_passing_preflight_explains_nothing() -> None:
    assert explain_preflight(PreflightResult(ok=True), "en") is None


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_both_locales_render_without_placeholder_leftovers(locale: str) -> None:
    """Every reason must be translated and fully substituted in both catalogs.

    ``translate`` raises on a placeholder it was not given and falls back to the
    raw key when an entry is absent, so this covers both classes of catalog bug
    across every kind at once.
    """
    for kind in FailureKind:
        for step in Step:
            rendered = explain_step(
                _outcome(step, kind, missing=("Ban Members",)), locale, channel_id=_CHANNEL
            )
            assert "failure." not in rendered, (locale, kind, step)
            assert "{" not in rendered, (locale, kind, step)
