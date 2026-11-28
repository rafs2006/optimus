"""Child-process entry point for the sandboxed OCR/QR risk scan.

Started by :mod:`optimus.services.detection.riskscan_sandbox` as
``python -m optimus.services.detection.riskscan_child '<json config>'``, with
the image bytes on stdin. It runs exactly one :func:`riskscan.scan` and exits,
so every buffer OpenCV allocated for the image goes back to the operating
system with the process.

The resource limits are applied here, before a single image byte is read,
rather than through ``preexec_fn`` in the parent: the parent is
multi-threaded, and running Python between ``fork`` and ``exec`` there can
deadlock. They are applied after the scanner (OpenCV, NumPy and their bundled
OpenBLAS) is imported, not before: under an address-space cap too small for
them, those native libraries do not fail with a Python exception but exit the
process themselves (OpenBLAS: "Memory allocation still failed after 10
retries") or hang, leaving no reason line and, at worst, a child that only the
wall timeout ends. Importing first costs no bound -- the address space and CPU
time the imports took count against the limits all the same -- and a limit
that does not even cover them fails at once, with a reason
(:func:`require_headroom`).

Protocol: one JSON object on stdout,
``{"findings": <OcrFindings dump or null>, "ocr": <OCR run report or null>}``.
Anything else a library prints to stdout is sent to stderr instead, so it
cannot corrupt the result.
"""

from __future__ import annotations

import json
import os
import resource
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prometheus_client import Counter, Histogram

FAILURE_PREFIX = "risk scan failed: "


def apply_limits(config: dict[str, Any]) -> None:
    """Cap this process (and the Tesseract processes it starts) per ``config``."""
    cpu = int(config["cpu_seconds"])
    mem = int(config["mem_bytes"])
    files = int(config["file_bytes"])
    _set_limit(resource.RLIMIT_CPU, cpu, cpu + 1)
    _set_limit(resource.RLIMIT_AS, mem, mem)
    # Not zero, unlike the hash decoder: pytesseract hands each preprocessed
    # variant to the tesseract binary through a temporary file.
    _set_limit(resource.RLIMIT_FSIZE, files, files)
    _set_limit(resource.RLIMIT_CORE, 0, 0)


def _set_limit(which: int, soft: int, hard: int) -> None:
    """Set a limit, never above a hard limit the host already imposes.

    Asking for more than the inherited hard limit is an error, and the
    tighter limit is the one that holds anyway.
    """
    inherited = resource.getrlimit(which)[1]
    if inherited != resource.RLIM_INFINITY:
        soft, hard = min(soft, inherited), min(hard, inherited)
    resource.setrlimit(which, (soft, hard))


def address_space_bytes() -> int | None:
    """This process's current address-space size (``VmSize``), if known."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmSize:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def require_headroom(config: dict[str, Any]) -> None:
    """Fail now if the memory limit does not even cover the loaded scanner.

    ``RLIMIT_AS`` may be set below what a process already uses; it then only
    makes every later allocation fail, in whichever library happens to make
    it. A limit that small can never scan anything, so say so plainly.
    """
    mem = int(config["mem_bytes"])
    used = address_space_bytes()
    if used is not None and used >= mem:
        mib = 1024 * 1024
        raise MemoryError(
            f"memory limit {mem // mib} MiB is below the {used // mib} MiB"
            " the loaded scanner already uses"
        )


def load_scanner() -> None:
    """Import the scan stack (OpenCV, NumPy) up front.

    The Tesseract bindings are small and still load on first use, under the cap.
    """
    import optimus.services.detection.riskscan  # noqa: F401


def ocr_report() -> dict[str, Any] | None:
    """Read back what the one OCR extraction of this process recorded.

    ``extract_text`` records its duration, variant count and outcome on the
    Prometheus series in this process, which nobody scrapes. Report them so the
    parent can replay them on its own series. ``None`` when OCR did not run
    exactly once.
    """
    from optimus.hashing.ocr_metrics import OCR_DURATION, OCR_OUTCOME, OCR_VARIANTS

    def total(metric: Counter | Histogram, suffix: str) -> float:
        return sum(
            sample.value
            for family in metric.collect()
            for sample in family.samples
            if sample.name.endswith(suffix)
        )

    outcomes = [
        sample.labels["outcome"]
        for family in OCR_OUTCOME.collect()
        for sample in family.samples
        if sample.name.endswith("_total") and sample.value > 0
    ]
    if len(outcomes) != 1 or total(OCR_OUTCOME, "_total") != 1:
        return None
    return {
        "seconds": total(OCR_DURATION, "_sum"),
        "variants": int(total(OCR_VARIANTS, "_sum")),
        "outcome": outcomes[0],
    }


def run(image_bytes: bytes, *, ocr_timeout: float | None) -> dict[str, Any]:
    """Scan one image and build the result object for the parent."""
    # Imported here, not at module import: this pulls in OpenCV, and the
    # parent imports this module (for FAILURE_PREFIX) without wanting it.
    from optimus.services.detection.riskscan import scan

    findings = scan(image_bytes, timeout=ocr_timeout)
    return {
        "findings": None if findings is None else findings.model_dump(),
        "ocr": ocr_report(),
    }


def describe_failure(exc: BaseException) -> str:
    """The one stderr line the parent repeats in the service log.

    Never image-derived text: a traceback or a validation error can quote
    what was read out of the image; an import, memory or OS error does not,
    and the first line of its message often tells a too-small memory limit
    apart from a broken install. The parent looks for the fixed prefix. A
    child that a native library aborts never gets here and leaves no line.
    """
    name = type(exc).__name__
    if isinstance(exc, ImportError | MemoryError | OSError):
        return f"{FAILURE_PREFIX}{name}: {exc}"
    return f"{FAILURE_PREFIX}{name}"


def main(argv: list[str]) -> int:
    config = json.loads(argv[1])
    # Keep the real stdout for the result and point fd 1 at stderr, so a stray
    # print from OpenCV, Tesseract or a logger cannot land in the JSON.
    result_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        # JSON log lines, like the service: the parent picks the event names
        # out of them, and the default console renderer would print local
        # variables with a traceback.
        from optimus.core.logging import configure_logging

        configure_logging(service_name="risk-scan")
        # Trusted code only until the limits are in force: no image byte has
        # been read yet. See the module docstring for why this order.
        load_scanner()
        require_headroom(config)
        apply_limits(config)
        result = run(sys.stdin.buffer.read(), ocr_timeout=config.get("ocr_timeout"))
    except Exception as exc:
        sys.stderr.write(describe_failure(exc) + "\n")
        return 1
    with os.fdopen(result_fd, "w", encoding="utf-8") as out:
        json.dump(result, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
