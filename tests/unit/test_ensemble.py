"""Tests for the ensemble verdict logic and sensitivity thresholds."""

from __future__ import annotations

import pytest

from optimus.contracts.events import Verdict
from optimus.core.config import Sensitivity
from optimus.hashing import ensemble


def _hashes(value: int = 0) -> dict[str, int]:
    return {"phash": value, "dhash": value, "whash": value, "ahash": value}


def test_identical_hashes_are_scam_with_full_confidence() -> None:
    result = ensemble.compare(_hashes(0), _hashes(0))
    assert result.verdict is Verdict.SCAM
    assert result.score == 0.0
    assert result.confidence == pytest.approx(1.0)


def test_distant_hashes_are_clean() -> None:
    candidate = _hashes(0)
    known = _hashes((1 << 64) - 1)  # maximal distance on every hash
    result = ensemble.compare(candidate, known)
    assert result.verdict is Verdict.CLEAN
    assert result.confidence == 0.0


def test_ambiguous_band_routes_between_scam_and_clean() -> None:
    preset = ensemble.PRESETS[Sensitivity.BALANCED]
    # Choose a uniform per-hash distance whose normalized score lands in the band.
    target_score = (preset.match_threshold + preset.ambiguous_ceiling) / 2
    bits = round(target_score * 64)
    known = _hashes((1 << bits) - 1)
    result = ensemble.compare(_hashes(0), known, Sensitivity.BALANCED)
    assert result.verdict is Verdict.AMBIGUOUS
    assert ensemble.is_ambiguous(result)


@pytest.mark.parametrize(
    "sensitivity",
    [Sensitivity.STRICT, Sensitivity.BALANCED, Sensitivity.PERMISSIVE],
)
def test_strict_is_most_permissive_threshold(sensitivity: Sensitivity) -> None:
    preset = ensemble.PRESETS[sensitivity]
    assert 0.0 < preset.match_threshold < 1.0
    assert preset.ambiguous_ceiling > preset.match_threshold


def test_strict_threshold_exceeds_permissive() -> None:
    strict = ensemble.PRESETS[Sensitivity.STRICT].match_threshold
    permissive = ensemble.PRESETS[Sensitivity.PERMISSIVE].match_threshold
    # "strict" tolerates more distance (catches more) than "permissive".
    assert strict > permissive


def test_distances_reported_per_hash() -> None:
    known = {"phash": 1, "dhash": 0, "whash": 0, "ahash": 0}
    result = ensemble.compare(_hashes(0), known)
    assert result.distances == {"phash": 1, "dhash": 0, "whash": 0, "ahash": 0}


def test_missing_overlap_yields_clean() -> None:
    result = ensemble.compare({"phash": 0}, {"dhash": 0})
    assert result.verdict is Verdict.CLEAN
