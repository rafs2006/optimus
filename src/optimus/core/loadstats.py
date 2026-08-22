"""A moderator-readable snapshot of how hard the bot is working.

``/metrics`` already exposes everything here, but reaching it needs a
Prometheus scrape, a hosted dashboard, and someone with access to the health
port -- none of which a server's moderators have. On a single-container
deployment the practical effect is that nobody can answer "is the bot keeping
up?" without shelling into the host.

This reads the numbers straight back out of the Prometheus registry the
workers already increment, so there is no second set of counters to keep in
sync and no extra bookkeeping on the hot path. Two consequences follow from
that and are deliberate:

* **The numbers are process-wide, not per-guild.** None of the pipeline
  counters carry a ``guild_id`` label (that would be unbounded cardinality),
  so this describes the whole bot across every server it is in. Callers must
  present it that way rather than implying it is the caller's own traffic.
* **They reset when the process restarts.** They are counters, not history.
  ``/stats`` pairs them with the boot count so the reset is visible.
"""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import REGISTRY, CollectorRegistry

#: Successfully fetched and validated images -- the "work done" number.
_SCANNED = "optimus_ingest_images_fetched_total"
#: Images that never reached decode. Each is a distinct failure mode worth
#: separating: a rejection is usually a bad or oversized image, a rate-limit
#: is the bot deliberately slowing down, and a drop is the per-message
#: attachment cap firing on a raid.
_REJECTED = "optimus_ingest_images_rejected_total"
_RATE_LIMITED = "optimus_ingest_rate_limited_total"
_DROPPED = "optimus_gateway_images_dropped_total"
#: Reached the detection worker but resolved without a real decision.
_DUPLICATE = "optimus_detection_duplicate_skipped_total"
_PAYLOAD_REJECTED = "optimus_detection_payload_rejected_total"
#: Work still queued for dispatch, summed across priority classes. A gauge,
#: so unlike the counters above this is a right-now value.
_QUEUE_DEPTH = "optimus_moderation_priority_queue_depth"


@dataclass(frozen=True, slots=True)
class LoadSnapshot:
    """Pipeline throughput since process start, plus the current queue depth.

    Every field is bot-wide. ``skipped`` is the total of the four skip
    reasons below it, pre-summed so a renderer can show a headline number
    without re-deriving it.
    """

    scanned: int
    queued: int
    skipped: int
    rejected: int
    rate_limited: int
    dropped: int
    duplicates: int


def _totals(registry: CollectorRegistry) -> dict[str, float]:
    """Sum every sample of every metric by sample name.

    Summing across label values is what makes this label-agnostic: a new
    ``reason=`` on a rejection counter or a new priority class on the queue
    gauge lands in the right bucket here without touching this module. The
    ``_created`` samples prometheus_client emits alongside counters are unix
    timestamps, not values, so they are skipped -- adding them would produce
    nonsense in the billions.
    """
    totals: dict[str, float] = {}
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name.endswith("_created"):
                continue
            totals[sample.name] = totals.get(sample.name, 0.0) + sample.value
    return totals


def load_snapshot(registry: CollectorRegistry = REGISTRY) -> LoadSnapshot:
    """Read the current pipeline load out of ``registry``.

    Missing metrics read as zero rather than raising: a counter that has
    never been incremented is simply absent from a scrape, and on a freshly
    started process most of these are. A worker that lives in another
    process entirely (the split-service deployment) is absent for the same
    reason, which is why this is documented as "this process".
    """
    totals = _totals(registry)

    def value(name: str) -> int:
        return int(totals.get(name, 0.0))

    rejected = value(_REJECTED)
    rate_limited = value(_RATE_LIMITED)
    dropped = value(_DROPPED)
    duplicates = value(_DUPLICATE) + value(_PAYLOAD_REJECTED)
    return LoadSnapshot(
        scanned=value(_SCANNED),
        queued=value(_QUEUE_DEPTH),
        skipped=rejected + rate_limited + dropped + duplicates,
        rejected=rejected,
        rate_limited=rate_limited,
        dropped=dropped,
        duplicates=duplicates,
    )
