"""Optional ONNX CPU embedding for ambiguous-band confirmation.

The embedding model is an optional extra (``optimus[embedding]``) and is gated by
``OPTIMUS_EMBEDDING_ENABLED``. ``onnxruntime`` is imported lazily so the core
detection path has no hard dependency on it.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import numpy.typing as npt

from optimus.core.config import get_settings
from optimus.core.logging import get_logger

_log = get_logger(__name__)

FloatArray = npt.NDArray[np.float64]


class EmbeddingUnavailableError(RuntimeError):
    """Raised when embedding is requested but disabled or not installed."""


def is_enabled() -> bool:
    """Whether embedding confirmation is enabled by configuration."""
    settings = get_settings()
    return settings.embedding_enabled and bool(settings.embedding_model_path)


@lru_cache(maxsize=1)
def _load_session() -> object:
    settings = get_settings()
    if not settings.embedding_enabled:
        raise EmbeddingUnavailableError("embedding is disabled (OPTIMUS_EMBEDDING_ENABLED)")
    if not settings.embedding_model_path:
        raise EmbeddingUnavailableError("OPTIMUS_EMBEDDING_MODEL_PATH is unset")
    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise EmbeddingUnavailableError(
            "onnxruntime not installed; install optimus[embedding]"
        ) from exc
    return ort.InferenceSession(settings.embedding_model_path, providers=["CPUExecutionProvider"])


def _preprocess(gray: FloatArray, size: int = 64) -> npt.NDArray[np.float32]:
    from optimus.hashing.perceptual import _resize_mean

    reduced = _resize_mean(gray, size, size) / 255.0
    return reduced.astype(np.float32).reshape(1, 1, size, size)


def embed(gray: FloatArray) -> FloatArray:
    """Return an L2-normalized embedding vector for a grayscale frame."""
    session = _load_session()
    inputs = {session.get_inputs()[0].name: _preprocess(gray)}  # type: ignore[attr-defined]
    raw = session.run(None, inputs)[0]  # type: ignore[attr-defined]
    vec = np.asarray(raw, dtype=np.float64).reshape(-1)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


def cosine_similarity(a: FloatArray, b: FloatArray) -> float:
    """Cosine similarity of two vectors in [-1, 1]."""
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)
