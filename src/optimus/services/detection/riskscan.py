"""OCR/QR risk scan: the second detection lane, for images no hash has seen.

Perceptual hashing only catches images we (or the global DB) have already
registered.  A brand-new scam image -- or a deliberately mutated variant far
enough outside the match radius -- sails through the hash lane.  This module
reads what the image *says* instead of what it *looks like*: Tesseract OCR
pulls the text, QR codes are decoded (never fetched), URLs are repaired from
defanging tricks (``hxxps://``, ``perplexity[.]com``), domains are checked
against lookalikes of official AI-company domains, and a weighted rule engine
scores the phishing signals.

The scan is advisory by design: the worker only ever escalates its result to
``AMBIGUOUS`` (mod queue, no action) -- an OCR heuristic must never delete,
ban, or store a hash on its own.  Zero trust: false positives land in front
of moderators, not on members.
"""

from __future__ import annotations

from optimus.contracts.events import OcrFindings
from optimus.hashing.ocr_extract import (
    _repair_urls,
    analyze_image,
    extract_urls,
    find_phishing_signals,
    is_lookalike,
    normalize_domain,
)
from optimus.hashing.qr_extract import extract_qr_urls

#: Cap the URLs carried onto the review card -- embed field values are small
#: and one hostile image could OCR into dozens of strings.
_MAX_CARD_URLS = 5


def scan(image_bytes: bytes) -> OcrFindings | None:
    """Run the full OCR + QR risk analysis on one image (blocking; run off-loop).

    Returns ``None`` when there is nothing noteworthy: no text signals, no QR
    payloads, no lookalike domains.  Never raises -- every underlying stage
    already degrades to empty output on failure.
    """
    analysis = analyze_image(image_bytes)
    qr_payloads = extract_qr_urls(image_bytes)

    urls: list[str] = list(analysis["urls"])
    lookalikes: list[dict[str, str]] = list(analysis["lookalikes"])
    for payload in qr_payloads:
        # QR payloads are arbitrary text; run them through the same repair +
        # extraction pipeline as OCR'd text so a defanged QR URL still parses.
        # QR codes also often encode a bare domain with no scheme, which the
        # URL regex won't match -- fall back to the whole payload then.
        repaired = _repair_urls(payload)
        candidates = extract_urls(repaired) or [repaired.strip()]
        for candidate in candidates:
            if not candidate:
                continue
            if candidate not in urls:
                urls.append(candidate)
            domain = normalize_domain(candidate)
            if not domain:
                continue
            target = is_lookalike(domain)
            if target and not any(entry["domain"] == domain for entry in lookalikes):
                lookalikes.append({"domain": domain, "impersonating": target, "url": candidate})

    # Re-score with the QR-augmented URL/lookalike sets so "scam text + QR
    # link" earns the same co-occurrence bonus as "scam text + typed link".
    signals, score, risk_level = find_phishing_signals(
        analysis["text"], urls=urls, lookalikes=lookalikes
    )

    # A lookalike domain is a strong signal even with no readable scam text
    # (e.g. a QR-only image pointing at perplexity-claim.com): floor it at
    # "high" so it reaches the mod queue instead of dying at "none".
    if lookalikes and risk_level in ("none", "low", "medium"):
        risk_level = "high"
        score = max(score, 4)
        if "lookalike_domain" not in signals:
            signals = [*signals, "lookalike_domain"]

    if risk_level == "none" and not qr_payloads:
        return None

    return OcrFindings(
        risk_level=risk_level,
        risk_score=score,
        signals=signals,
        lookalike_domains=[
            f"{entry['domain']} \u2192 {entry['impersonating']}" for entry in lookalikes
        ][:_MAX_CARD_URLS],
        qr_urls=qr_payloads[:_MAX_CARD_URLS],
    )
