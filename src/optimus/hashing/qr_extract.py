"""QR code extraction from images using OpenCV's built-in detector."""

from __future__ import annotations

import cv2
import numpy as np

from optimus.core.logging import get_logger

_log = get_logger(__name__)


def extract_qr_urls(image_bytes: bytes) -> list[str]:
    """Decode QR codes from raw image bytes. Returns decoded strings (URLs/text).

    Decode only -- the payload is never fetched, so a hostile QR (SSRF-style
    internal URL, javascript:, etc.) is inert text handed to the risk scorer.
    """
    try:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return []
        detector = cv2.QRCodeDetector()
        data, _points, _ = detector.detectAndDecode(img)
        if data:
            return [data]
        # Try multi-detect for images with several QR codes
        ok, decoded, _, _ = detector.detectAndDecodeMulti(img)
        if ok:
            return [d for d in decoded if d]
    except Exception:
        _log.warning("qr_extract_failed", exc_info=True)
    return []
