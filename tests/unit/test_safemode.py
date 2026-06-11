"""Unit tests for safe-mode anomaly detection (EWMA + variance)."""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from hypothesis import given
from hypothesis import strategies as st

from optimus.services.moderation.safemode import (
    Baseline,
    SafeModeTracker,
    evaluate,
    update_baseline,
)


def test_update_baseline_tracks_mean() -> None:
    b = Baseline()
    for _ in range(50):
        b = update_baseline(b, 10.0, alpha=0.3)
    assert b.mean == pytest.approx(10.0, abs=0.5)
    assert b.samples == 50


def test_update_baseline_rejects_bad_alpha() -> None:
    with pytest.raises(ValueError, match="alpha"):
        update_baseline(Baseline(), 1.0, alpha=0.0)
    with pytest.raises(ValueError, match="alpha"):
        update_baseline(Baseline(), 1.0, alpha=1.5)


def test_no_anomaly_during_warmup() -> None:
    # A huge spike on the very first observation must not trip (no history).
    decision = evaluate(Baseline(), 1000.0, sigma=4.0, alpha=0.3, min_floor=5.0)
    assert not decision.is_anomaly


def test_no_anomaly_below_min_floor() -> None:
    # Establish a low-rate baseline; a spike that is still tiny stays quiet.
    b = Baseline()
    for _ in range(10):
        b = update_baseline(b, 1.0, alpha=0.3)
    decision = evaluate(b, 3.0, sigma=4.0, alpha=0.3, min_floor=5.0)
    assert not decision.is_anomaly


def test_anomaly_trips_above_threshold() -> None:
    b = Baseline()
    for _ in range(30):
        b = update_baseline(b, 10.0, alpha=0.3)
    # Baseline mean is ~10, variance ~0; a 10x spike is clearly anomalous.
    decision = evaluate(b, 200.0, sigma=4.0, alpha=0.3, min_floor=5.0)
    assert decision.is_anomaly


def test_steady_state_does_not_trip() -> None:
    b = Baseline()
    for _ in range(30):
        b = update_baseline(b, 10.0, alpha=0.3)
    decision = evaluate(b, 11.0, sigma=4.0, alpha=0.3, min_floor=5.0)
    assert not decision.is_anomaly


def test_evaluate_rejects_bad_sigma() -> None:
    with pytest.raises(ValueError, match="sigma"):
        evaluate(Baseline(), 1.0, sigma=0.0, alpha=0.3, min_floor=5.0)


def test_baseline_json_roundtrip() -> None:
    b = Baseline(mean=12.5, variance=3.25, samples=7)
    assert Baseline.from_json(b.to_json()) == b


@given(st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False))
def test_property_variance_never_negative(observation: float) -> None:
    b = Baseline(mean=50.0, variance=10.0, samples=5)
    b2 = update_baseline(b, observation, alpha=0.3)
    assert b2.variance >= 0.0
    assert b2.stddev >= 0.0


async def test_tracker_persists_and_flags() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    tracker = SafeModeTracker(redis, sigma=4.0, alpha=0.3, min_floor=5.0)
    for _ in range(30):
        decision = await tracker.observe(1, 10.0)
        assert not decision.is_anomaly
    spike = await tracker.observe(1, 300.0)
    assert spike.is_anomaly
    # Baseline persisted across calls.
    assert spike.baseline.samples == 31
