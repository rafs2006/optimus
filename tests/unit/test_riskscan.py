"""Tests for the OCR/QR risk-scan lane: scoring, and worker escalation.

``riskscan.scan`` tests monkeypatch ``analyze_image``/``extract_qr_urls`` so no
Tesseract binary is needed; the scoring pipeline underneath (URL repair,
lookalike detection, signal rules) runs for real. Worker tests inject a fake
scanner -- the worker contract is about *when* the scan runs and *what* it does
to the verdict, not about OCR quality.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from optimus.contracts.events import ImageFetchedEvent, OcrFindings, Verdict
from optimus.services.detection import riskscan
from optimus.services.detection.index import HashIndex
from optimus.services.detection.worker import DetectionWorker

EMPTY = HashIndex([])


def _analysis(
    *,
    text: str = "",
    urls: list[str] | None = None,
    lookalikes: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "text": text,
        "urls": urls or [],
        "lookalikes": lookalikes or [],
        "signals": [],
        "risk_score": 0,
        "risk_level": "none",
    }


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    analysis: dict[str, object],
    qr: list[str] | None = None,
) -> None:
    monkeypatch.setattr(riskscan, "analyze_image", lambda _b, **_kw: analysis)
    monkeypatch.setattr(riskscan, "extract_qr_urls", lambda _b: qr or [])


# --- riskscan.scan -----------------------------------------------------------


def test_scan_nothing_found_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, analysis=_analysis())
    assert riskscan.scan(b"img") is None


def test_scan_scores_scam_text(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "FREE nitro giveaway! Act now, connect your wallet and verify your account"
    _patch(monkeypatch, analysis=_analysis(text=text))
    findings = riskscan.scan(b"img")
    assert findings is not None
    assert findings.risk_level in ("high", "critical")
    assert "wallet_connect" in findings.signals or findings.risk_score >= 4


def test_scan_qr_lookalike_floors_to_high(monkeypatch: pytest.MonkeyPatch) -> None:
    # QR-only image: no readable text at all, but the QR encodes a lookalike
    # of an official AI domain. Must not die at "none".
    _patch(monkeypatch, analysis=_analysis(), qr=["https://perplexity-claim.com/verify"])
    findings = riskscan.scan(b"img")
    assert findings is not None
    assert findings.risk_level == "high"
    assert "lookalike_domain" in findings.signals
    assert any("perplexity" in d for d in findings.lookalike_domains)
    assert findings.qr_urls == ["https://perplexity-claim.com/verify"]


def test_scan_defanged_bare_domain_qr(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bare defanged domain payload (no scheme): repair + fallback path.
    _patch(monkeypatch, analysis=_analysis(), qr=["openai-support[.]com"])
    findings = riskscan.scan(b"img")
    assert findings is not None
    assert findings.risk_level == "high"
    assert any("openai.com" in d for d in findings.lookalike_domains)


def test_scan_benign_qr_reported_but_low(monkeypatch: pytest.MonkeyPatch) -> None:
    # A QR pointing somewhere unremarkable: findings carry the payload for
    # the card, but the risk stays below the escalation bar.
    _patch(monkeypatch, analysis=_analysis(), qr=["https://example.com/menu"])
    findings = riskscan.scan(b"img")
    assert findings is not None
    assert findings.risk_level in ("none", "low", "medium")


def test_scan_caps_card_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    qr = [f"https://example{i}.com" for i in range(12)]
    _patch(monkeypatch, analysis=_analysis(), qr=qr)
    findings = riskscan.scan(b"img")
    assert findings is not None
    assert len(findings.qr_urls) <= 5


# --- worker escalation -------------------------------------------------------


def _png() -> bytes:
    """A real decodable PNG: undecodable payloads early-return NON_DECISION
    before the risk-scan lane (an image Pillow cannot decode is one Discord
    will not render to a victim either)."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _event(*, key: str = "k1", data: bytes | None = None) -> ImageFetchedEvent:
    data = data if data is not None else _png()
    return ImageFetchedEvent(
        correlation_id="c",
        occurred_at=datetime.now(UTC),
        guild_id=1,
        channel_id=2,
        message_id=3,
        attachment_id=4,
        uploader_id=5,
        idempotency_key=key,
        content_type="image/png",
        size_bytes=len(data),
        sha256="0" * 64,
        data_b64=base64.b64encode(data).decode(),
    )


class _Guard:
    async def acquire(self, key: str) -> bool:
        return True


class _Scanner:
    """Fake risk scanner recording calls and returning canned findings."""

    def __init__(self, findings: OcrFindings | None) -> None:
        self.findings = findings
        self.calls = 0

    def __call__(self, data: bytes) -> OcrFindings | None:
        self.calls += 1
        return self.findings


def _findings(level: str, score: int = 7) -> OcrFindings:
    return OcrFindings(risk_level=level, risk_score=score, signals=["credential_harvest"])


def _worker(scanner: _Scanner | None) -> DetectionWorker:
    async def gi(_gid: int) -> HashIndex:
        return EMPTY

    async def gx() -> HashIndex:
        return EMPTY

    async def wl(_gid: int) -> list:  # type: ignore[type-arg]
        return []

    async def sens(_gid: int):
        from optimus.core.config import Sensitivity

        return Sensitivity.BALANCED

    return DetectionWorker(
        guild_index=gi,
        global_index=gx,
        whitelist=wl,
        sensitivity=sens,
        idempotency_acquire=_Guard().acquire,
        risk_scan=scanner,
    )


async def test_worker_escalates_critical_to_ambiguous() -> None:
    scanner = _Scanner(_findings("critical", score=9))
    result = await _worker(scanner).handle(_event())
    assert result is not None
    assert scanner.calls == 1
    assert result.verdict.verdict is Verdict.AMBIGUOUS
    assert result.verdict.confidence == 0.9
    assert result.verdict.ocr is not None
    assert result.verdict.ocr.risk_level == "critical"


async def test_worker_escalates_high_with_lower_confidence() -> None:
    scanner = _Scanner(_findings("high", score=5))
    result = await _worker(scanner).handle(_event())
    assert result is not None
    assert result.verdict.verdict is Verdict.AMBIGUOUS
    assert result.verdict.confidence == 0.75


async def test_worker_ignores_medium_and_low() -> None:
    for level in ("low", "medium"):
        scanner = _Scanner(_findings(level, score=2))
        result = await _worker(scanner).handle(_event(key=f"k-{level}"))
        assert result is not None
        assert scanner.calls == 1
        assert result.verdict.verdict is not Verdict.AMBIGUOUS
        assert result.verdict.ocr is None


async def test_worker_without_scanner_unchanged() -> None:
    result = await _worker(None).handle(_event())
    assert result is not None
    assert result.verdict.ocr is None


async def test_worker_scanner_none_result_unchanged() -> None:
    scanner = _Scanner(None)
    result = await _worker(scanner).handle(_event())
    assert result is not None
    assert scanner.calls == 1
    assert result.verdict.verdict is not Verdict.AMBIGUOUS
    assert result.verdict.ocr is None


# --- review card rendering ---------------------------------------------------


def test_ocr_summary_rendering() -> None:
    from optimus.services.moderation.coordinator import _ocr_summary

    assert _ocr_summary(None) is None
    findings = OcrFindings(
        risk_level="critical",
        risk_score=9,
        signals=["credential_harvest", "crypto_address"],
        lookalike_domains=["perplexity-claim.com \u2192 perplexity.ai"],
        qr_urls=["https://evil.example/qr"],
    )
    summary = _ocr_summary(findings)
    assert summary is not None
    assert "critical (score 9)" in summary
    assert "credential_harvest" in summary
    assert "perplexity-claim.com" in summary
    assert "`https://evil.example/qr`" in summary


def test_ocr_summary_truncated() -> None:
    from optimus.services.moderation.coordinator import _OCR_SUMMARY_MAX, _ocr_summary

    findings = OcrFindings(
        risk_level="high",
        risk_score=5,
        signals=[f"signal_{i}" * 40 for i in range(20)],
    )
    summary = _ocr_summary(findings)
    assert summary is not None
    assert len(summary) <= _OCR_SUMMARY_MAX + 1


def test_report_fields_include_ocr_summary() -> None:
    from optimus.services.moderation.review import ReportData, report_fields

    data = ReportData(
        detection_id=7,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=4,
        verdict="ambiguous",
        confidence=0.9,
        action_taken="report_only",
        ocr_summary="critical (score 9) | signals: credential_harvest",
    )
    fields = dict(report_fields(data))
    assert any("critical (score 9)" in v for v in fields.values())
    # And absent when no OCR findings drove the verdict.
    plain = ReportData(
        detection_id=8,
        guild_id=1,
        channel_id=2,
        message_id=3,
        uploader_id=4,
        verdict="scam",
        confidence=1.0,
        action_taken="delete",
    )
    assert not any("OCR" in name for name, _ in report_fields(plain))
