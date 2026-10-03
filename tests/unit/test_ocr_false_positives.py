"""The OCR risk scan must not send ordinary screenshots to the mod queue.

Reported on a real server: the bot raised review cards for practically every
image with text in it. Three scoring rules compounded:

- URL repair deleted every space, so "Thanks for the help. Our team is on it"
  became ``Thanksforthehelp.Ourteamisonit`` -- a "URL" worth 3 points.
- A link to the real ``perplexity.ai`` or ``discord.com`` scored the same 3
  points as a scam link.
- One weak word ("team", "support", "free") plus any URL reached 4, "high".

"High" is the escalation bar, so each of these became a card. The scam cases
at the bottom pin the other half: real lures still escalate.

``riskscan.scan`` is the production entry point; ``analyze_image`` is patched
so no Tesseract binary is needed, and everything after OCR runs for real.
"""

from __future__ import annotations

import pytest

from optimus.hashing.ocr_extract import (
    _repair_urls,
    analyze_image,
    extract_urls,
    find_phishing_signals,
)
from optimus.services.detection import riskscan

ESCALATES = ("high", "critical")


def _scan(monkeypatch: pytest.MonkeyPatch, text: str) -> str:
    """Risk level the production scan assigns to an image that OCRs to ``text``."""
    monkeypatch.setattr("optimus.hashing.ocr_extract.extract_text", lambda _b, **_kw: text)
    monkeypatch.setattr(riskscan, "analyze_image", analyze_image)
    monkeypatch.setattr(riskscan, "extract_qr_urls", lambda _b: [])
    findings = riskscan.scan(b"img")
    return "none" if findings is None else findings.risk_level


ORDINARY = [
    pytest.param("Thanks for the help. Our team is on it", id="sentence-join"),
    pytest.param("Answer\nThe staff. Check settings", id="line-and-sentence-join"),
    pytest.param("Thanks. Support is great. Got it. To fix, see docs", id="dot-before-tld-word"),
    pytest.param(
        "Perplexity Pro\nperplexity.ai/search/how-to-cook\nAnswer Sources", id="answer-page"
    ),
    pytest.param(
        "Perplexity Support\nHi, our team will get back to you\nhelp.perplexity.ai",
        id="support-chat",
    ),
    pytest.param("#general  Server Staff  discord.com/channels/123", id="discord-screenshot"),
    pytest.param(
        "Account settings  Sign in with Google  perplexity.ai/settings", id="settings-page"
    ),
    pytest.param("Get your free month of Pro\nperplexity.ai/pro", id="official-promo"),
    pytest.param("Team update: new release notes at github.com/rafs2006", id="weak-word-and-link"),
    pytest.param("Free webinar, limited seats: example.com/event", id="marketing-and-link"),
]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_screenshots_do_not_escalate(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    assert _scan(monkeypatch, text) not in ESCALATES


SCAMS = [
    pytest.param("Claim your free Nitro at discord-gift.xyz", id="nitro-lure"),
    pytest.param(
        "Official support: verify your account at perplexity-support.com/login",
        id="lookalike-login",
    ),
    pytest.param("Enter your seed phrase to restore wallet: walletfix.io", id="seed-phrase"),
    pytest.param(
        "Send ETH to 0x52908400098527886E0F7030069857D2E4169EE7 to receive 2x", id="crypto"
    ),
    pytest.param("FREE Perplexity Pro! Claim now: hxxps://perplexity-pro[.]xyz", id="defanged"),
    pytest.param(
        "You have been selected! Redeem your gift at gift-claim . com / redeem", id="spaced-dot"
    ),
    pytest.param("Free Pro, limited time: perplexity-gift dot com", id="dot-word-lookalike"),
]


@pytest.mark.parametrize("text", SCAMS)
def test_scam_lures_still_escalate(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    assert _scan(monkeypatch, text) in ESCALATES


@pytest.mark.parametrize(
    "text",
    [
        "Thanks for the help. Our team is on it",
        "I love it.Sources are great",  # missed space, but "sources" is no domain ending
        "see image.png and notes.txt, v1.2 e.g. this",
        "polka dot dress",
    ],
)
def test_prose_yields_no_urls(text: str) -> None:
    assert extract_urls(_repair_urls(text)) == []


@pytest.mark.parametrize(
    ("text", "url"),
    [
        ("openai[.]com", "openai.com"),
        ("openai(.)com", "openai.com"),
        ("openai[dot]com", "openai.com"),
        ("openai dot com", "openai.com"),
        ("openai . com", "openai.com"),
        ("go to scam.example.xyz now", "scam.example.xyz"),
        ("WWW.Example.Shop", "WWW.Example.Shop"),
    ],
)
def test_defanged_and_bare_domains_still_parse(text: str, url: str) -> None:
    assert url in extract_urls(_repair_urls(text))


def test_official_link_earns_no_url_bonus() -> None:
    _, with_official, _ = find_phishing_signals("claim", urls=["perplexity.ai/pro"])
    _, without, _ = find_phishing_signals("claim")
    assert with_official == without


def test_weak_words_beside_a_link_earn_a_small_bonus_only() -> None:
    _, score, level = find_phishing_signals("our team", urls=["example.com"])
    assert (score, level) == (2, "medium")


def test_strong_signal_beside_a_link_earns_the_full_bonus() -> None:
    _, score, level = find_phishing_signals("redeem", urls=["example.com"])
    assert (score, level) == (5, "high")


def test_unparseable_url_still_counts_as_untrusted() -> None:
    # Fail toward scoring: a URL whose domain cannot be parsed is not trusted.
    _, score, _ = find_phishing_signals("redeem", urls=["https://\u202eperplexity.ai.example"])
    assert score == 5
