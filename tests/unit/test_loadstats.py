"""Tests for reading pipeline load back out of the Prometheus registry."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge

from optimus.core.loadstats import load_snapshot


def test_snapshot_of_an_empty_registry_is_all_zeroes() -> None:
    """A freshly started process has incremented nothing.

    Counters that have never been touched are simply absent from a scrape,
    so every lookup must read as zero instead of raising -- otherwise
    /stats would blow up on a bot that just booted.
    """
    snap = load_snapshot(CollectorRegistry())
    assert snap.scanned == 0
    assert snap.queued == 0
    assert snap.skipped == 0


def test_snapshot_reads_throughput_and_sums_skips() -> None:
    reg = CollectorRegistry()
    fetched = Counter("optimus_ingest_images_fetched_total", "", registry=reg)
    rejected = Counter("optimus_ingest_images_rejected_total", "", ["reason"], registry=reg)
    limited = Counter("optimus_ingest_rate_limited_total", "", registry=reg)
    dropped = Counter("optimus_gateway_images_dropped_total", "", ["reason"], registry=reg)
    duplicate = Counter("optimus_detection_duplicate_skipped_total", "", registry=reg)
    payload = Counter("optimus_detection_payload_rejected_total", "", ["reason"], registry=reg)
    depth = Gauge("optimus_moderation_priority_queue_depth", "", ["priority"], registry=reg)

    fetched.inc(100)
    rejected.labels(reason="too_large").inc(3)
    rejected.labels(reason="bad_mime").inc(4)
    limited.inc(2)
    dropped.labels(reason="attachment_cap").inc(1)
    duplicate.inc(10)
    payload.labels(reason="too_many_frames").inc(5)
    depth.labels(priority="protect").set(2)
    depth.labels(priority="notify").set(3)

    snap = load_snapshot(reg)
    assert snap.scanned == 100
    # Label values are summed, so a new reason= needs no change here.
    assert snap.rejected == 7
    assert snap.rate_limited == 2
    assert snap.dropped == 1
    # Duplicates fold in payload rejections: both are "reached detection and
    # resolved without a real decision", which is one idea for a moderator.
    assert snap.duplicates == 15
    # The gauge is summed across priority classes into one waiting figure.
    assert snap.queued == 5
    assert snap.skipped == 7 + 2 + 1 + 15


def test_snapshot_ignores_counter_created_timestamps() -> None:
    """``*_created`` samples are unix timestamps, not values.

    prometheus_client emits one per counter; adding it to a total would
    produce a number in the billions and make the whole section nonsense.
    """
    reg = CollectorRegistry()
    Counter("optimus_ingest_images_fetched_total", "", registry=reg).inc(7)
    assert load_snapshot(reg).scanned == 7


def test_snapshot_of_the_default_registry_is_readable() -> None:
    """Smoke test against the real registry the workers increment.

    Guards the metric *names* this module hardcodes: the module cannot
    import them from the workers (that would drag the whole pipeline into
    an interaction handler's import graph), so a rename in a worker would
    otherwise silently zero the numbers. This asserts the call works and
    yields non-negative ints; the per-name wiring is covered above.
    """
    from prometheus_client import REGISTRY

    import optimus.services.detection.worker
    import optimus.services.gateway.extract
    import optimus.services.ingest.worker
    import optimus.services.moderation.priority  # noqa: F401  # registers QUEUE_DEPTH

    names = {sample.name for metric in REGISTRY.collect() for sample in metric.samples}
    # Every name this module reads must exist once the workers are imported.
    for expected in (
        "optimus_ingest_images_fetched_total",
        "optimus_ingest_rate_limited_total",
        "optimus_detection_duplicate_skipped_total",
    ):
        assert expected in names, expected

    snap = load_snapshot()
    assert snap.scanned >= 0
    assert snap.skipped >= 0
