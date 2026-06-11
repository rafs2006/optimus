"""Tests for the sandboxed image decoder."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from optimus.hashing import perceptual as ph
from optimus.hashing.decoder import DecodeLimits, decode


def _png_bytes(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb.astype(np.uint8), mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def test_decode_returns_grayscale_frame() -> None:
    rng = np.random.default_rng(3)
    rgb = rng.integers(0, 256, (64, 80, 3), dtype=np.uint8)
    result = decode(_png_bytes(rgb))
    assert result is not None
    assert result.width == 80
    assert result.height == 64
    assert len(result.frames) == 1
    assert result.frames[0].shape == (64, 80)


def test_decode_garbage_is_non_decision() -> None:
    assert decode(b"not an image at all") is None


def test_decode_respects_pixel_cap() -> None:
    rgb = np.zeros((100, 100, 3), dtype=np.uint8)
    limits = DecodeLimits(max_image_pixels=100)  # 10_000 pixels exceeds cap
    assert decode(_png_bytes(rgb), limits) is None


def test_decode_animated_samples_multiple_frames() -> None:
    frames = [
        Image.fromarray((np.full((32, 32, 3), v, dtype=np.uint8)), mode="RGB")
        for v in (10, 120, 240, 60)
    ]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=50, loop=0)
    result = decode(buf.getvalue(), DecodeLimits(max_frames=4))
    assert result is not None
    assert result.frame_count >= 2
    assert len(result.frames) >= 2


def test_decoded_frame_hashes() -> None:
    rng = np.random.default_rng(9)
    rgb = rng.integers(0, 256, (96, 96, 3), dtype=np.uint8)
    result = decode(_png_bytes(rgb))
    assert result is not None
    hashes = ph.compute_all(result.frames[0])
    for v in hashes.values():
        assert 0 <= v < (1 << 64)


@pytest.mark.parametrize("timeout", [0.000001])
def test_decode_timeout_is_non_decision(timeout: float) -> None:
    rgb = np.zeros((512, 512, 3), dtype=np.uint8)
    assert decode(_png_bytes(rgb), DecodeLimits(wall_timeout=timeout)) is None
