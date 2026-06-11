"""Unit tests for the moderation policy engine (full decision matrix)."""

from __future__ import annotations

import pytest

from optimus.contracts.events import Action, Verdict
from optimus.services.moderation.policy import (
    Decision,
    PolicyInput,
    PolicyOutcome,
    decide,
)


def _inp(**kw: object) -> PolicyInput:
    base: dict[str, object] = {
        "verdict": Verdict.SCAM,
        "confidence": 0.9,
        "configured_action": Action.DELETE_BAN,
        "mod_queue_threshold": 0.5,
        "auto_act_threshold": 0.85,
        "safe_mode": False,
    }
    base.update(kw)
    return PolicyInput(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize("verdict", [Verdict.CLEAN, Verdict.NON_DECISION])
def test_non_decidable_verdicts_do_nothing(verdict: Verdict) -> None:
    out = decide(_inp(verdict=verdict))
    assert out == PolicyOutcome(Decision.NONE, Action.NONE, f"verdict_{verdict.value}")


def test_below_queue_threshold_is_none() -> None:
    out = decide(_inp(confidence=0.4))
    assert out.decision is Decision.NONE
    assert out.action is Action.NONE


def test_between_thresholds_queues() -> None:
    out = decide(_inp(confidence=0.6))
    assert out.decision is Decision.MOD_QUEUE
    assert out.action is Action.REPORT_ONLY
    assert out.reason == "queued_for_review"


def test_ambiguous_never_auto_acts_even_when_confident() -> None:
    out = decide(_inp(verdict=Verdict.AMBIGUOUS, confidence=0.99))
    assert out.decision is Decision.MOD_QUEUE
    assert out.action is Action.REPORT_ONLY


def test_scam_above_auto_threshold_auto_acts() -> None:
    out = decide(_inp(confidence=0.9, configured_action=Action.DELETE_BAN))
    assert out.decision is Decision.AUTO_ACT
    assert out.action is Action.DELETE_BAN


@pytest.mark.parametrize(
    "configured",
    [Action.DELETE, Action.DELETE_TIMEOUT, Action.DELETE_KICK, Action.DELETE_BAN],
)
def test_auto_act_honours_configured_action(configured: Action) -> None:
    out = decide(_inp(confidence=0.95, configured_action=configured))
    assert out.decision is Decision.AUTO_ACT
    assert out.action is configured


@pytest.mark.parametrize("configured", [Action.NONE, Action.REPORT_ONLY])
def test_report_only_policy_downgrades_to_queue(configured: Action) -> None:
    out = decide(_inp(confidence=0.95, configured_action=configured))
    assert out.decision is Decision.MOD_QUEUE
    assert out.action is Action.REPORT_ONLY
    assert out.reason == "policy_report_only"


def test_safe_mode_forces_report_only() -> None:
    out = decide(_inp(confidence=0.99, configured_action=Action.DELETE_BAN, safe_mode=True))
    assert out.decision is Decision.MOD_QUEUE
    assert out.action is Action.REPORT_ONLY
    assert out.reason == "safe_mode_report_only"


def test_at_exact_auto_threshold_auto_acts() -> None:
    out = decide(_inp(confidence=0.85, auto_act_threshold=0.85))
    assert out.decision is Decision.AUTO_ACT


def test_at_exact_queue_threshold_queues() -> None:
    out = decide(_inp(confidence=0.5, mod_queue_threshold=0.5, auto_act_threshold=0.85))
    assert out.decision is Decision.MOD_QUEUE


def test_invalid_threshold_ordering_raises() -> None:
    with pytest.raises(ValueError, match="auto_act_threshold"):
        decide(_inp(mod_queue_threshold=0.9, auto_act_threshold=0.5))
