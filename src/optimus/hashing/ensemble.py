"""Weighted-vote ensemble combining the four perceptual hashes into a verdict.

Admins choose a sensitivity preset (strict/balanced/permissive); raw Hamming
distances are never surfaced. Each preset defines a match threshold and an
ambiguous band just below it that routes to optional embedding confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass

from optimus.contracts.events import Verdict
from optimus.core.config import Sensitivity
from optimus.hashing.perceptual import HASH_BITS, hamming

# Relative trust placed in each hash family when voting.
DEFAULT_WEIGHTS: dict[str, float] = {
    "phash": 0.40,
    "dhash": 0.30,
    "whash": 0.20,
    "ahash": 0.10,
}


@dataclass(frozen=True, slots=True)
class Preset:
    """Sensitivity preset.

    ``match_threshold`` is the maximum weighted-average normalized distance at
    which an image counts as a scam match. The ``ambiguous_band`` extends the
    threshold upward into a region that is flagged ambiguous rather than clean.
    """

    match_threshold: float
    ambiguous_band: float

    @property
    def ambiguous_ceiling(self) -> float:
        """Upper edge of the ambiguous region."""
        return self.match_threshold + self.ambiguous_band


PRESETS: dict[Sensitivity, Preset] = {
    Sensitivity.STRICT: Preset(match_threshold=0.18, ambiguous_band=0.06),
    Sensitivity.BALANCED: Preset(match_threshold=0.12, ambiguous_band=0.05),
    Sensitivity.PERMISSIVE: Preset(match_threshold=0.08, ambiguous_band=0.04),
}


@dataclass(frozen=True, slots=True)
class EnsembleResult:
    """Outcome of comparing a candidate hash set against a known scam hash set."""

    verdict: Verdict
    confidence: float
    score: float
    distances: dict[str, int]


def _normalized_weighted_score(distances: dict[str, int], weights: dict[str, float]) -> float:
    """Return the weighted average of per-hash normalized distances in [0, 1]."""
    total_weight = 0.0
    acc = 0.0
    for name, dist in distances.items():
        w = weights.get(name, 0.0)
        if w == 0.0:
            continue
        acc += w * (dist / HASH_BITS)
        total_weight += w
    if total_weight == 0.0:
        return 1.0
    return acc / total_weight


def compare(
    candidate: dict[str, int],
    known: dict[str, int],
    sensitivity: Sensitivity = Sensitivity.BALANCED,
    *,
    weights: dict[str, float] | None = None,
) -> EnsembleResult:
    """Compare a candidate hash set to a known scam hash set.

    Returns a :class:`EnsembleResult` whose verdict is ``SCAM`` below the
    preset threshold, ``AMBIGUOUS`` within the band, else ``CLEAN``.
    """
    w = weights or DEFAULT_WEIGHTS
    preset = PRESETS[sensitivity]
    shared = candidate.keys() & known.keys()
    distances = {name: hamming(candidate[name], known[name]) for name in shared}
    score = _normalized_weighted_score(distances, w)

    if score <= preset.match_threshold:
        verdict = Verdict.SCAM
    elif score <= preset.ambiguous_ceiling:
        verdict = Verdict.AMBIGUOUS
    else:
        verdict = Verdict.CLEAN

    # Confidence: 1.0 at distance 0, decaying to 0 at the ambiguous ceiling.
    ceiling = preset.ambiguous_ceiling
    confidence = max(0.0, min(1.0, 1.0 - (score / ceiling))) if ceiling > 0 else 0.0
    return EnsembleResult(verdict=verdict, confidence=confidence, score=score, distances=distances)


def is_ambiguous(result: EnsembleResult) -> bool:
    """Whether a result falls in the ambiguous band (embedding confirmation)."""
    return result.verdict is Verdict.AMBIGUOUS
