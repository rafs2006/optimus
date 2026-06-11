"""Property and unit tests for the perceptual hash pipeline."""

from __future__ import annotations

import io

import numpy as np
import numpy.typing as npt
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from optimus.hashing import perceptual as ph

_U64 = st.integers(min_value=0, max_value=(1 << 64) - 1)


@given(a=_U64, b=_U64)
def test_hamming_symmetry_and_bounds(a: int, b: int) -> None:
    d = ph.hamming(a, b)
    assert d == ph.hamming(b, a)
    assert 0 <= d <= ph.HASH_BITS


@given(a=_U64)
def test_hamming_identity(a: int) -> None:
    assert ph.hamming(a, a) == 0


@given(a=_U64, b=_U64, c=_U64)
def test_hamming_triangle_inequality(a: int, b: int, c: int) -> None:
    assert ph.hamming(a, c) <= ph.hamming(a, b) + ph.hamming(b, c)


def test_hamming_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        ph.hamming(-1, 0)
    with pytest.raises(ValueError):
        ph.hamming(1 << 64, 0)


def _to_array(img: Image.Image) -> npt.NDArray[np.uint8]:
    return np.asarray(img.convert("RGB"), dtype=np.uint8)


def _reencode(rgb: npt.NDArray[np.uint8], quality: int) -> npt.NDArray[np.uint8]:
    im = Image.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return _to_array(Image.open(buf))


def test_all_hashes_are_uint64(gradient_image: npt.NDArray[np.uint8]) -> None:
    gray = ph.to_grayscale(gradient_image)
    hashes = ph.compute_all(gray)
    assert set(hashes) == {"ahash", "dhash", "phash", "whash"}
    for value in hashes.values():
        assert 0 <= value < (1 << 64)


def test_hashes_stable_under_jpeg_reencode(gradient_image: npt.NDArray[np.uint8]) -> None:
    gray = ph.to_grayscale(gradient_image)
    gray2 = ph.to_grayscale(_reencode(gradient_image, quality=85))
    for name in ("phash", "dhash", "whash", "ahash"):
        fn = getattr(ph, name)
        original = fn(gray)
        reencoded = fn(gray2)
        # Re-encoding must keep the hash within a small Hamming radius.
        assert ph.hamming(original, reencoded) <= 8, name


def test_phash_distinguishes_different_images() -> None:
    rng = np.random.default_rng(7)
    a = rng.integers(0, 256, (128, 128), dtype=np.uint8).astype(np.float64)
    b = rng.integers(0, 256, (128, 128), dtype=np.uint8).astype(np.float64)
    assert ph.phash(a) != ph.phash(b)


def test_to_grayscale_rejects_bad_shape() -> None:
    with pytest.raises(ValueError):
        ph.to_grayscale(np.zeros((4, 4, 2), dtype=np.uint8))


@settings(max_examples=25, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=10_000),
    size=st.integers(min_value=16, max_value=64),
)
def test_resize_mean_output_shape(seed: int, size: int) -> None:
    rng = np.random.default_rng(seed)
    gray = rng.integers(0, 256, (size, size), dtype=np.uint8).astype(np.float64)
    out = ph._resize_mean(gray, 8, 8)
    assert out.shape == (8, 8)
    assert np.isfinite(out).all()
