"""Tests for the risk-scan child's helpers.

``main`` itself only runs in a real child process (it re-points this process's
stdout and applies resource limits); the end-to-end tests in
``test_riskscan_sandbox`` cover it.
"""

from __future__ import annotations

import resource
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry, Counter, Histogram

from optimus.contracts.events import OcrFindings
from optimus.hashing import ocr_metrics
from optimus.services.detection import riskscan, riskscan_child


@pytest.fixture
def fresh_series(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the test its own, empty OCR series, as a new child process has."""
    registry = CollectorRegistry()
    monkeypatch.setattr(
        ocr_metrics, "OCR_DURATION", Histogram("ocr_duration_seconds", "d", registry=registry)
    )
    monkeypatch.setattr(
        ocr_metrics, "OCR_VARIANTS", Histogram("ocr_variants", "v", registry=registry)
    )
    monkeypatch.setattr(
        ocr_metrics, "OCR_OUTCOME", Counter("ocr_outcome", "o", ["outcome"], registry=registry)
    )


@pytest.mark.usefixtures("fresh_series")
def test_ocr_report_reads_back_the_single_run() -> None:
    ocr_metrics.record_ocr_run(seconds=2.5, variants=3, outcome="complete")
    assert riskscan_child.ocr_report() == {"seconds": 2.5, "variants": 3, "outcome": "complete"}


@pytest.mark.usefixtures("fresh_series")
def test_ocr_report_is_none_when_ocr_did_not_run() -> None:
    assert riskscan_child.ocr_report() is None


@pytest.mark.usefixtures("fresh_series")
def test_ocr_report_is_none_when_ocr_ran_more_than_once() -> None:
    ocr_metrics.record_ocr_run(seconds=1.0, variants=1, outcome="complete")
    ocr_metrics.record_ocr_run(seconds=1.0, variants=0, outcome="error")
    assert riskscan_child.ocr_report() is None
    ocr_metrics.record_ocr_run(seconds=1.0, variants=1, outcome="complete")
    assert riskscan_child.ocr_report() is None


@pytest.mark.usefixtures("fresh_series")
def test_run_returns_findings_and_report(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_scan(data: bytes, *, timeout: float | None = None) -> OcrFindings:
        seen.update(data=data, timeout=timeout)
        ocr_metrics.record_ocr_run(seconds=0.5, variants=3, outcome="complete")
        return OcrFindings(risk_level="high", risk_score=5, signals=["claim"])

    monkeypatch.setattr(riskscan, "scan", fake_scan)
    result = riskscan_child.run(b"img", ocr_timeout=4.0)
    assert seen == {"data": b"img", "timeout": 4.0}
    assert result["findings"]["risk_level"] == "high"
    assert result["findings"]["signals"] == ["claim"]
    assert result["ocr"] == {"seconds": 0.5, "variants": 3, "outcome": "complete"}


@pytest.mark.usefixtures("fresh_series")
def test_run_reports_nothing_noteworthy_as_null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(riskscan, "scan", lambda _data, *, timeout=None: None)
    assert riskscan_child.run(b"img", ocr_timeout=None) == {"findings": None, "ocr": None}


def test_apply_limits_sets_every_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[int, tuple[int, int]] = {}
    unlimited = (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
    monkeypatch.setattr(resource, "getrlimit", lambda _which: unlimited)
    monkeypatch.setattr(resource, "setrlimit", lambda which, value: calls.update({which: value}))
    riskscan_child.apply_limits({"cpu_seconds": 28, "mem_bytes": 1024, "file_bytes": 2048})
    assert calls == {
        resource.RLIMIT_CPU: (28, 29),
        resource.RLIMIT_AS: (1024, 1024),
        resource.RLIMIT_FSIZE: (2048, 2048),
        resource.RLIMIT_CORE: (0, 0),
    }


def test_apply_limits_stays_within_an_inherited_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that already caps the process (``ulimit``) must not break the scan."""
    calls: dict[int, tuple[int, int]] = {}
    inherited = {resource.RLIMIT_CPU: 20, resource.RLIMIT_AS: 512, resource.RLIMIT_FSIZE: 100}
    monkeypatch.setattr(
        resource,
        "getrlimit",
        lambda which: (resource.RLIM_INFINITY, inherited.get(which, resource.RLIM_INFINITY)),
    )
    monkeypatch.setattr(resource, "setrlimit", lambda which, value: calls.update({which: value}))
    riskscan_child.apply_limits({"cpu_seconds": 38, "mem_bytes": 1024, "file_bytes": 2048})
    assert calls == {
        resource.RLIMIT_CPU: (20, 20),
        resource.RLIMIT_AS: (512, 512),
        resource.RLIMIT_FSIZE: (100, 100),
        resource.RLIMIT_CORE: (0, 0),
    }


def test_describe_failure_keeps_only_content_free_messages() -> None:
    describe = riskscan_child.describe_failure
    assert describe(MemoryError()) == "risk scan failed: MemoryError: "
    assert describe(ImportError("failed to map segment")) == (
        "risk scan failed: ImportError: failed to map segment"
    )
    assert describe(OSError("no space left")) == "risk scan failed: OSError: no space left"
    # Anything else may quote text read out of the image: the type name only.
    assert describe(ValueError("claim your reward at ...")) == "risk scan failed: ValueError"


def test_require_headroom_fails_a_limit_the_loaded_scanner_already_exceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below what is already mapped, every later allocation would fail somewhere."""
    monkeypatch.setattr(riskscan_child, "address_space_bytes", lambda: 300 * 1024 * 1024)
    with pytest.raises(MemoryError, match="memory limit 48 MiB is below the 300 MiB"):
        riskscan_child.require_headroom({"mem_bytes": 48 * 1024 * 1024})
    riskscan_child.require_headroom({"mem_bytes": 2048 * 1024 * 1024})


def test_require_headroom_lets_an_unknown_size_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``/proc`` (not Linux): the limit itself still holds."""
    monkeypatch.setattr(riskscan_child, "address_space_bytes", lambda: None)
    riskscan_child.require_headroom({"mem_bytes": 1024})


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs /proc (Linux)")
def test_address_space_bytes_reads_this_process() -> None:
    size = riskscan_child.address_space_bytes()
    assert size is not None
    assert size > 0
