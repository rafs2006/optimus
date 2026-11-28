"""Prometheus series for the OCR lane.

Kept apart from :mod:`optimus.hashing.ocr_extract` so a process can record
these series without importing OpenCV: the OCR itself runs in a short-lived
child process (see :mod:`optimus.services.detection.riskscan_sandbox`), and
the long-lived parent only replays the numbers the child reports.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

# The OCR lane is the most expensive stage in the pipeline and the only one with
# no experiment behind its cost. These three series exist so the budget default
# can be argued from data rather than from a single hand-timed image: duration
# says what the lane actually costs, variants says whether the multi-pass design
# is being truncated in practice, and outcome says how often the budget is the
# binding constraint.
OCR_DURATION = Histogram(
    "optimus_ocr_duration_seconds",
    "Wall-clock time for one full multi-pass OCR extraction.",
    # Straddles the budget (8s) so budget-bound calls pile into a visible bucket
    # instead of the overflow; the low end resolves cheap early exits.
    buckets=(0.05, 0.25, 1.0, 2.5, 4.0, 6.0, 8.0, 12.0, 20.0),
)
OCR_VARIANTS = Histogram(
    "optimus_ocr_variants_completed",
    "Preprocessing variants that finished OCR before the budget ran out.",
    # There are only ever 3 variants; integer bounds make p50 readable directly.
    buckets=(0, 1, 2, 3),
)
OCR_OUTCOME = Counter(
    "optimus_ocr_outcome_total",
    "How each OCR extraction terminated.",
    ["outcome"],
)

#: Every value ``extract_text`` can put in the ``outcome`` label. The parent
#: checks a child's report against this set, so a child that was subverted by
#: a hostile image cannot mint new label values.
OCR_OUTCOMES: frozenset[str] = frozenset({"complete", "budget_exhausted", "decode_failed", "error"})
#: ``extract_text`` builds at most this many preprocessing variants.
OCR_MAX_VARIANTS = 3


def record_ocr_run(*, seconds: float, variants: int, outcome: str) -> None:
    """Record one finished OCR extraction on all three series."""
    OCR_DURATION.observe(seconds)
    OCR_VARIANTS.observe(variants)
    OCR_OUTCOME.labels(outcome=outcome).inc()
