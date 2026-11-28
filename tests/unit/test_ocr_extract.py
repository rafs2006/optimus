"""Tests for OCR text extraction, URL extraction, and domain lookalike detection."""

from collections.abc import Iterator

import pytest

from optimus.hashing.ocr_extract import (
    OFFICIAL_AI_DOMAINS,
    analyze_image,
    extract_urls,
    is_lookalike,
    normalize_domain,
)


def test_extract_urls_from_text():
    text = "Claim free Pro at https://perplexity-claim.com/offer now!"
    urls = extract_urls(text)
    assert len(urls) == 1
    assert (
        "perplexity-claim.com/offer" in urls[0] or "https://perplexity-claim.com/offer" in urls[0]
    )


def test_extract_urls_dedupes():
    text = "Visit openai.com and openai.com today"
    urls = extract_urls(text)
    assert len(urls) == 1


def test_extract_urls_handles_empty():
    assert extract_urls("") == []
    assert extract_urls("no urls here") == []


def test_normalize_domain_strips_www():
    assert normalize_domain("www.openai.com") == "openai.com"


def test_normalize_domain_lowercases():
    assert normalize_domain("OpenAI.COM/path") == "openai.com"


def test_normalize_domain_handles_bare_domain():
    assert normalize_domain("perplexity.ai") == "perplexity.ai"


def test_normalize_domain_uses_hostname_not_userinfo():
    assert normalize_domain("https://openai.com@evil.example/path") == "evil.example"


def test_normalize_domain_returns_empty_for_invalid_idna():
    assert normalize_domain("https://\u202eopenai.com.example") == ""


def test_analyze_image_ignores_invalid_idna_domain(monkeypatch):
    monkeypatch.setattr(
        "optimus.hashing.ocr_extract.extract_text",
        lambda _, **_kw: "Claim now at https://\u202eopenai.com.example",
    )
    analysis = analyze_image(b"ignored")
    assert analysis["urls"] == ["https://\u202eopenai.com.example"]
    assert analysis["lookalikes"] == []


def test_lookalike_exact_match_is_safe():
    assert is_lookalike("openai.com") is None
    assert is_lookalike("perplexity.ai") is None


def test_lookalike_subdomain_of_official_is_safe():
    assert is_lookalike("api.openai.com") is None


def test_lookalike_detects_prefix():
    assert is_lookalike("openai-claim.com") == "openai.com"


def test_lookalike_detects_tld_swap():
    result = is_lookalike("openai.ru")
    assert result is not None
    assert "openai" in result


def test_lookalike_detects_close_typos():
    assert is_lookalike("openai.con") == "openai.com"


def test_lookalike_returns_none_for_unrelated():
    assert is_lookalike("example.com") is None
    assert is_lookalike("github.com") is None


def test_lookalike_empty_domain():
    assert is_lookalike("") is None


class _Backwards(frozenset[str]):
    """A set that hands out its members last-first, as another process might."""

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(frozenset(self), reverse=True))


def test_lookalike_target_does_not_follow_set_order() -> None:
    """Two official domains qualify here; set order must not pick between them.

    A set's order follows the per-process string hash seed, so the domain
    named as impersonated used to change from one process to the next.
    """
    official = frozenset({"perplexity.ai", "perplexity.com"})
    assert is_lookalike("perplexity-claim.com", official) == "perplexity.ai"
    assert is_lookalike("perplexity-claim.com", _Backwards(official)) == "perplexity.ai"


@pytest.mark.parametrize(
    "domain",
    ["chatgpt.openai.com", "grok.x.ai", "deepmind.google.com", "claude.anthropic.com"],
)
def test_subdomain_of_an_official_domain_is_never_a_lookalike(domain: str) -> None:
    """Also when its first label matches another official domain's name.

    ``chatgpt.openai.com`` is under the official ``openai.com`` and shares a
    label with the official ``chatgpt.com``; whichever the loop met first used
    to decide.
    """
    parent = domain.split(".", 1)[1]
    assert parent in OFFICIAL_AI_DOMAINS
    assert is_lookalike(domain) is None
    assert is_lookalike(domain, _Backwards(OFFICIAL_AI_DOMAINS)) is None
