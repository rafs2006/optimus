"""Tests for embedding helpers that do not require an ONNX model."""

from __future__ import annotations

import numpy as np

from optimus.core.config import get_settings
from optimus.hashing import embedding


def test_cosine_similarity_basic() -> None:
    a = np.array([1.0, 0.0, 0.0])
    b = np.array([1.0, 0.0, 0.0])
    c = np.array([0.0, 1.0, 0.0])
    assert embedding.cosine_similarity(a, b) == 1.0
    assert embedding.cosine_similarity(a, c) == 0.0


def test_cosine_similarity_zero_vector() -> None:
    a = np.zeros(3)
    b = np.array([1.0, 2.0, 3.0])
    assert embedding.cosine_similarity(a, b) == 0.0


def test_is_enabled_reflects_settings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    get_settings.cache_clear()
    monkeypatch.setenv("OPTIMUS_EMBEDDING_ENABLED", "false")
    assert embedding.is_enabled() is False
    get_settings.cache_clear()
    monkeypatch.setenv("OPTIMUS_EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("OPTIMUS_EMBEDDING_MODEL_PATH", "/tmp/model.onnx")
    assert embedding.is_enabled() is True
    get_settings.cache_clear()
