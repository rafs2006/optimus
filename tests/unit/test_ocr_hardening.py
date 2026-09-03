"""Tests for OCR hardening: preprocessing, URL repair, phishing signals."""

from optimus.hashing.ocr_extract import (
    _preprocess_variants,
    _repair_urls,
    find_phishing_signals,
)


def test_repair_urls_defanged():
    text = "Visit hxxps://openai[.]com/claim now"
    assert "https://openai.com/claim" in _repair_urls(text)


def test_repair_urls_spaces_in_domain():
    text = "Go to open ai . com / login"
    repaired = _repair_urls(text)
    assert "openai.com" in repaired


def test_repair_urls_dot_parentheses():
    text = "perplexity (.) com scam"
    assert "perplexity.com" in _repair_urls(text)


def test_repair_urls_dot_word():
    text = "free openai dot com claim"
    assert "openai.com" in _repair_urls(text)


def test_repair_urls_plain_text_unchanged():
    text = "This is normal text without urls"
    assert _repair_urls(text) == "Thisisnormaltextwithouturls"


def test_phishing_signals_free_offer():
    signals, _, _ = find_phishing_signals("Claim your FREE Pro account now!")
    assert "free_offer" in signals
    assert "claim" in signals


def test_phishing_signals_urgency():
    signals, _, _ = find_phishing_signals("Limited time! Expires in 1 hour. Act now!")
    assert "urgency" in signals


def test_phishing_signals_credentials():
    signals, _, _ = find_phishing_signals("Please verify your account and login")
    assert "credentials" in signals


def test_phishing_signals_ai_community():
    signals, _, _ = find_phishing_signals("Get free Sora access and GPT-5 beta!")
    assert "ai_community" in signals
    assert "free_offer" in signals


def test_phishing_signals_impersonation():
    signals, _, _ = find_phishing_signals("Official support team message")
    assert "impersonation" in signals


def test_phishing_signals_clean_text():
    signals, _, _ = find_phishing_signals("Hello, how are you today?")
    assert signals == []


def test_phishing_signals_multiple_categories():
    signals, _, _ = find_phishing_signals(
        "FREE airdrop! Limited time! Connect wallet to claim your reward. Official admin support."
    )
    assert "free_offer" in signals
    assert "urgency" in signals
    assert "credentials" in signals
    assert "impersonation" in signals


def test_phishing_signals_empty_text():
    signals, score, level = find_phishing_signals("")
    assert signals == []
    assert score == 0
    assert level == "none"


def test_preprocess_variants_returns_multiple():
    import numpy as np

    img = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
    variants = _preprocess_variants(img)
    assert len(variants) >= 2
    for v in variants:
        assert v.ndim == 2


def test_preprocess_variants_upscales_small():
    import numpy as np

    img = np.random.randint(0, 255, (200, 200, 3), dtype=np.uint8)
    variants = _preprocess_variants(img)
    for v in variants:
        assert v.shape[0] >= 400  # upscaled 2x


def test_preprocess_variants_downscales_huge():
    import numpy as np

    img = np.random.randint(0, 255, (5000, 5000, 3), dtype=np.uint8)
    variants = _preprocess_variants(img)
    for v in variants:
        assert max(v.shape) <= 4000


def test_preprocess_variants_upscales_midsize_screenshot():
    """A 1148px-wide collage must still be enlarged before OCR.

    Regression: upscaling used to be gated on ``max(shape) < 1000``, so a
    multi-panel scam screenshot -- large overall, but with tiny text in each
    panel -- fell in the gap between that gate and the downscale cap and was
    handed to Tesseract at native scale. It OCR'd to noise, scoring "low"
    instead of "critical", and the review card was never raised.
    """
    import numpy as np

    img = np.random.randint(0, 255, (756, 1148, 3), dtype=np.uint8)
    variants = _preprocess_variants(img)
    assert variants, "expected at least one preprocessing variant"
    for v in variants:
        assert max(v.shape) > 1148, "mid-size image was left at native scale"


def test_preprocess_variants_normalize_longest_edge_both_directions():
    """Every image, large or small, is normalised to the same longest edge."""
    import numpy as np

    from optimus.hashing.ocr_extract import _MAX_OCR_DIM

    for shape in ((120, 300, 3), (756, 1148, 3), (3000, 1000, 3)):
        variants = _preprocess_variants(np.random.randint(0, 255, shape, dtype=np.uint8))
        for v in variants:
            assert max(v.shape) == _MAX_OCR_DIM


def test_preprocess_variants_preserve_aspect_ratio():
    import numpy as np

    variants = _preprocess_variants(np.random.randint(0, 255, (400, 800, 3), dtype=np.uint8))
    for v in variants:
        h, w = v.shape[:2]
        assert abs((w / h) - 2.0) < 0.02


def test_preprocess_grayscale_input():
    import numpy as np

    gray = np.random.randint(0, 255, (500, 500), dtype=np.uint8)
    variants = _preprocess_variants(gray)
    assert len(variants) >= 2


def test_extract_text_budget_is_shared_across_every_variant(monkeypatch):
    """Each variant must get a slice of the budget, not be starved by the first.

    Regression: the total budget was 3.0s while a single variant of a
    normalised image costs ~2.5s, so variant 1 consumed almost all of it and
    the CLAHE/Otsu passes -- the ones that rescue low-contrast dark-mode
    screenshots -- were skipped. Only the weakest pass survived.
    """
    import cv2
    import numpy as np

    from optimus.hashing import ocr_extract

    img = np.random.randint(0, 255, (300, 400, 3), dtype=np.uint8)
    encoded = cv2.imencode(".png", img)[1].tobytes()
    variant_count = len(ocr_extract._preprocess_variants(img))
    assert variant_count >= 3, "expected the multi-pass variant set"

    seen: list[float] = []

    def _fake(_img, *, timeout):
        seen.append(timeout)
        return f"text-{len(seen)}"

    monkeypatch.setattr(ocr_extract, "_ocr_confident_text", _fake)
    out = ocr_extract.extract_text(encoded, timeout=9.0)

    assert len(seen) == variant_count, "a variant was starved of budget"
    assert all(t > 0 for t in seen)
    assert seen[0] <= 9.0
    for text in (f"text-{i + 1}" for i in range(variant_count)):
        assert text in out


def test_extract_text_stops_once_the_budget_is_spent(monkeypatch):
    """A slow first variant must not be followed by zero-budget OCR calls."""
    import cv2
    import numpy as np

    from optimus.hashing import ocr_extract

    img = np.random.randint(0, 255, (300, 400, 3), dtype=np.uint8)
    encoded = cv2.imencode(".png", img)[1].tobytes()
    calls: list[float] = []
    clock = iter([0.0, 0.0, 50.0, 50.0, 50.0, 50.0])

    def _fake(_img, *, timeout):
        calls.append(timeout)
        return "only-pass"

    monkeypatch.setattr(ocr_extract, "_ocr_confident_text", _fake)
    monkeypatch.setattr(ocr_extract.time, "monotonic", lambda: next(clock))
    ocr_extract.extract_text(encoded, timeout=8.0)

    assert len(calls) == 1, "kept calling OCR after the budget was exhausted"


def test_ocr_timeout_setting_defaults_above_single_pass_cost():
    """The configured budget must fit the whole variant set, not one pass."""
    from optimus.core.config import Settings
    from optimus.hashing.ocr_extract import _OCR_TOTAL_TIMEOUT_SECONDS

    settings = Settings(discord_token="x")
    assert settings.detection_ocr_timeout_seconds == _OCR_TOTAL_TIMEOUT_SECONDS
    assert settings.detection_ocr_timeout_seconds >= 8.0


# ---------------------------------------------------------------------------
# Resize deadband
# ---------------------------------------------------------------------------


def test_preprocess_variants_skips_resize_inside_deadband():
    """An image already within the deadband is handed to OCR untouched.

    Normalising 1584px -> 1600px is a full INTER_CUBIC pass over every pixel
    for a 1% scale change, which cannot resolve a glyph that was unreadable
    before. Screenshots cluster on stock display widths, so this band carries
    real traffic.
    """
    import numpy as np

    from optimus.hashing.ocr_extract import _MAX_OCR_DIM, _OCR_DIM_DEADBAND

    longest = _MAX_OCR_DIM - (_OCR_DIM_DEADBAND - 1)
    variants = _preprocess_variants(np.random.randint(0, 255, (900, longest, 3), dtype=np.uint8))
    for v in variants:
        assert max(v.shape) == longest, "resized despite being inside the deadband"


def test_preprocess_variants_resizes_just_outside_deadband():
    """One pixel past the deadband and normalisation applies as usual.

    Pins the boundary from the other side so the deadband cannot silently
    widen into the range where upscaling still buys real recall.
    """
    import numpy as np

    from optimus.hashing.ocr_extract import _MAX_OCR_DIM, _OCR_DIM_DEADBAND

    longest = _MAX_OCR_DIM - _OCR_DIM_DEADBAND
    variants = _preprocess_variants(np.random.randint(0, 255, (900, longest, 3), dtype=np.uint8))
    for v in variants:
        assert max(v.shape) == _MAX_OCR_DIM, "skipped a resize outside the deadband"


def test_preprocess_variants_deadband_applies_to_oversized_images_too():
    """The deadband is symmetric: a slightly-too-large image is left alone."""
    import numpy as np

    from optimus.hashing.ocr_extract import _MAX_OCR_DIM, _OCR_DIM_DEADBAND

    longest = _MAX_OCR_DIM + (_OCR_DIM_DEADBAND - 1)
    variants = _preprocess_variants(np.random.randint(0, 255, (900, longest, 3), dtype=np.uint8))
    for v in variants:
        assert max(v.shape) == longest, "shrank an image inside the deadband"


# ---------------------------------------------------------------------------
# Per-call metrics
# ---------------------------------------------------------------------------


def _outcome_count(outcome: str) -> float:
    """Current value of the outcome counter, 0 when the series is unseeded."""
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value("optimus_ocr_outcome_total", {"outcome": outcome})
    return 0.0 if value is None else value


def _variants_observed() -> tuple[float, float]:
    """(count, sum) of the variants-completed histogram."""
    from prometheus_client import REGISTRY

    count = REGISTRY.get_sample_value("optimus_ocr_variants_completed_count")
    total = REGISTRY.get_sample_value("optimus_ocr_variants_completed_sum")
    return (count or 0.0, total or 0.0)


def _encoded_image():
    import cv2
    import numpy as np

    img = np.random.randint(0, 255, (756, 1148, 3), dtype=np.uint8)
    return cv2.imencode(".png", img)[1].tobytes()


def _decode(data: bytes):
    import cv2
    import numpy as np

    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


def test_extract_text_records_decode_failure():
    """A payload that is not an image is counted, not silently dropped."""
    from optimus.hashing import ocr_extract

    before = _outcome_count("decode_failed")
    assert ocr_extract.extract_text(b"definitely not an image") == ""
    assert _outcome_count("decode_failed") == before + 1


def test_extract_text_records_completion_and_variant_count(monkeypatch):
    """A clean run reports every variant it finished."""
    from optimus.hashing import ocr_extract

    monkeypatch.setattr(ocr_extract, "_ocr_confident_text", lambda _img, *, timeout: "text")
    encoded = _encoded_image()
    expected_variants = len(ocr_extract._preprocess_variants(_decode(encoded)))

    before_outcome = _outcome_count("complete")
    before_count, before_sum = _variants_observed()
    ocr_extract.extract_text(encoded, timeout=9.0)
    after_count, after_sum = _variants_observed()

    assert _outcome_count("complete") == before_outcome + 1
    assert after_count == before_count + 1
    assert after_sum - before_sum == expected_variants


def test_extract_text_records_budget_exhaustion(monkeypatch):
    """Budget exhaustion is the one failure mode the 8s default is tuned on.

    It used to be a bare ``break`` -- invisible, so there was no way to tell a
    healthy multi-pass run from one truncated to its weakest variant.
    """
    import time

    from optimus.hashing import ocr_extract

    def _slow(_img, *, timeout):
        time.sleep(0.05)
        return "text"

    monkeypatch.setattr(ocr_extract, "_ocr_confident_text", _slow)

    before = _outcome_count("budget_exhausted")
    ocr_extract.extract_text(_encoded_image(), timeout=0.04)
    assert _outcome_count("budget_exhausted") == before + 1
