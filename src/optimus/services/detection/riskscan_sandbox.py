"""Run the OCR/QR risk scan in a short-lived, resource-limited child process.

The scan decodes the image at full resolution with OpenCV (up to
``max_image_pixels``, 24 MP by default), builds several preprocessing variants
and runs Tesseract and the QR detector over them. Doing that on threads inside
a long-lived process has two costs:

* Memory. The allocator keeps the freed image buffers in per-thread arenas
  instead of returning them, so the resident size of the process climbs to
  several times the cost of one scan and stays there for as long as the process
  runs. A child process hands everything back when it exits.
* No bounds. Untrusted bytes were parsed by OpenCV inside the service process
  with no memory, CPU or time limit, so a hostile image could take the whole
  service down with it. The hash lane has always decoded in a limited child
  (:mod:`optimus.hashing.decoder`); this gives the risk-scan lane the same
  treatment.

The limits bound resources; they are not an isolation boundary. The child runs
as the same user as the service, so code execution inside it could still reach
whatever that user can (files, the network, the parent's ``/proc`` entry).

Any failure -- the child could not start, ran out of time or memory, crashed,
or answered with something unexpected -- yields ``None``, the same "nothing
noteworthy" the in-process scan returned when a stage failed. The scan is
advisory and must never fail a detection.

This module must not import OpenCV, directly or through
:mod:`optimus.services.detection.riskscan`: keeping it out of the parent is the
point.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from prometheus_client import Counter, Histogram

import optimus
from optimus.contracts.events import OcrFindings
from optimus.core.logging import get_logger
from optimus.hashing.ocr_metrics import OCR_MAX_VARIANTS, OCR_OUTCOMES, record_ocr_run
from optimus.services.detection.riskscan_child import FAILURE_PREFIX

_log = get_logger(__name__)

_CHILD_MODULE = "optimus.services.detection.riskscan_child"

#: Seconds the child gets on top of the OCR budget for everything that is not
#: Tesseract: interpreter start, importing OpenCV, the full-resolution decode
#: and QR detection. QR detection is the open-ended part: on synthetic 24 MP
#: JPEGs it took from a few seconds to well over ten on an idle core, and it
#: had no limit at all before. Generous on purpose, so a slow or busy host does
#: not start losing findings that the in-process scan used to deliver.
_OVERHEAD_SECONDS = 30.0
#: ``_OCR_TOTAL_TIMEOUT_SECONDS`` in optimus.hashing.ocr_extract, which cannot
#: be imported here without OpenCV. A test keeps the two equal.
_DEFAULT_OCR_SECONDS = 8.0

SCAN_CHILD_OUTCOME = Counter(
    "optimus_risk_scan_child_total",
    "How each sandboxed risk-scan child process ended.",
    ["outcome"],
)
SCAN_CHILD_DURATION = Histogram(
    "optimus_risk_scan_child_seconds",
    "Wall-clock time of one sandboxed risk scan, process start to exit.",
    buckets=(0.25, 0.5, 1.0, 2.5, 5.0, 8.0, 12.0, 18.0, 30.0, 45.0, 60.0),
)


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """Resource limits applied to one risk-scan child process."""

    #: Total OCR budget across all preprocessing variants, enforced inside the
    #: child by ``extract_text``. ``None`` uses the ocr_extract default.
    ocr_timeout: float | None = None
    #: Wall clock for the whole child. On expiry the child and the Tesseract
    #: process it may have running are killed.
    wall_timeout: float = _DEFAULT_OCR_SECONDS + _OVERHEAD_SECONDS
    #: Address-space cap (``RLIMIT_AS``), inherited by Tesseract. Synthetic
    #: 24 MP JPEGs needed somewhere between 512 and 768 MB with the thread
    #: pools pinned (see _CHILD_ENV_OVERRIDES), depending on the content;
    #: below that the decode or the QR pass fails and the scan degrades to
    #: "nothing found". This leaves well over 2x headroom.
    mem_bytes: int = 2048 * 1024 * 1024
    #: Largest single file the child may write (``RLIMIT_FSIZE``; a per-file
    #: cap, not a quota). pytesseract passes each <=1600 px variant to
    #: Tesseract as a temporary file.
    file_bytes: int = 64 * 1024 * 1024
    #: A result larger than this is discarded unread. A real one is a risk
    #: level, a few signal names and at most a handful of URLs.
    max_output_bytes: int = 64 * 1024

    @classmethod
    def for_ocr_timeout(cls, ocr_timeout: float) -> ScanLimits:
        """Limits for a given OCR budget, with the wall clock derived from it."""
        return cls(ocr_timeout=ocr_timeout, wall_timeout=ocr_timeout + _OVERHEAD_SECONDS)


#: Single-threaded numeric libraries in the child. The work is one image, the
#: thread pools are sized against the host's CPU count rather than the
#: container's quota, and every extra thread costs address space under
#: ``RLIMIT_AS`` (see the same pinning in optimus.hashing.decoder).
#: ``OMP_THREAD_LIMIT`` is the one Tesseract honours. ``MALLOC_ARENA_MAX``
#: keeps glibc from reserving a 64 MB arena per thread.
_CHILD_ENV_OVERRIDES = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "OMP_THREAD_LIMIT": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "OPENCV_FOR_THREADS_NUM": "1",
    "MALLOC_ARENA_MAX": "2",
}
#: Left out of the child's environment: the service settings (``OPTIMUS_*``,
#: the bot token among them) and cloud credentials for the evidence store.
#: The child needs none of them. This keeps them out of the scanner's own
#: memory and of anything it execs; it does not hide them from code running
#: as the same user (see the module docstring).
_SECRET_ENV_PREFIXES = ("OPTIMUS_", "AWS_")

#: Event names are code constants, so they are safe to repeat in the service
#: log; the rest of a child log line can quote what was read from the image.
_EVENT_NAME = re.compile(r"[a-z0-9_]{1,64}")
_REPORTED_LEVELS = frozenset({"warning", "error", "critical"})
_STDERR_READ_BYTES = 16 * 1024
_MAX_CHILD_EVENTS = 8


def _child_env(tmpdir: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SECRET_ENV_PREFIXES)}
    env.update(_CHILD_ENV_OVERRIDES)
    # pytesseract's temporary files go where the parent can remove them even
    # when the child is killed in the middle of OCR.
    env["TMPDIR"] = tmpdir
    # ``python -m`` must find this same package even when the parent got it
    # from a path that is not inherited (a test runner's sys.path entry).
    package_root = str(Path(optimus.__file__).resolve().parents[1])
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{package_root}{os.pathsep}{inherited}" if inherited else package_root
    return env


def _child_argv(limits: ScanLimits) -> list[str]:
    config = {
        "ocr_timeout": limits.ocr_timeout,
        "cpu_seconds": math.ceil(limits.wall_timeout),
        "mem_bytes": limits.mem_bytes,
        "file_bytes": limits.file_bytes,
    }
    return [sys.executable, "-m", _CHILD_MODULE, json.dumps(config)]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the child and anything it started (it leads its own session).

    Also called after a normal exit, where the child is already reaped and
    only a Tesseract it left behind can still be in the group. The group id is
    the child's pid; Linux hands out pids cyclically, so it is not given to an
    unrelated process in the instant between the reap and this call.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


def _child_said(err: IO[bytes]) -> tuple[list[str], str | None, int]:
    """The parts of the child's stderr that are safe to repeat in the log.

    Returns the names of the warnings and errors it logged (for example
    ``qr_extract_failed``, which no longer reaches the service log by itself),
    the one-line reason :func:`riskscan_child.describe_failure` wrote, if any,
    and how many bytes the child wrote in all. Tracebacks and everything else
    stay behind; the byte count says whether there was something to look for
    when a child died without a reason line (a native library aborting, say).
    """
    size = err.seek(0, os.SEEK_END)
    start = max(0, size - _STDERR_READ_BYTES)
    err.seek(start)
    lines = err.read().decode("utf-8", errors="replace").splitlines()
    if start:
        # The read began mid-line; what is left of that line is not a line.
        lines = lines[1:]
    events: list[str] = []
    reason: str | None = None
    for line in lines:
        if line.startswith(FAILURE_PREFIX):
            reason = line[len(FAILURE_PREFIX) :][:200]
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("level") not in _REPORTED_LEVELS:
            continue
        event = entry.get("message")
        if (
            isinstance(event, str)
            and _EVENT_NAME.fullmatch(event)
            and event not in events
            and len(events) < _MAX_CHILD_EVENTS
        ):
            events.append(event)
    return events, reason, size


def _replay_ocr_metrics(report: Any, limits: ScanLimits) -> None:
    """Record the child's OCR run on this process's series, if it is sane."""
    if not isinstance(report, dict):
        return
    seconds, variants, outcome = (report.get(k) for k in ("seconds", "variants", "outcome"))
    # The range checks also reject NaN and infinities, which compare false.
    if (
        not isinstance(seconds, int | float)
        or isinstance(seconds, bool)
        or not 0 <= seconds <= limits.wall_timeout
        or not isinstance(variants, int)
        or isinstance(variants, bool)
        or not 0 <= variants <= OCR_MAX_VARIANTS
        or not isinstance(outcome, str)
        or outcome not in OCR_OUTCOMES
    ):
        return
    record_ocr_run(seconds=float(seconds), variants=variants, outcome=outcome)


def _parse(raw: bytes, limits: ScanLimits) -> OcrFindings | None:
    """Turn the child's stdout into findings. Raises on anything unexpected."""
    if len(raw) > limits.max_output_bytes:
        raise ValueError("result too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("result is not an object")
    findings = payload.get("findings")
    result = None if findings is None else OcrFindings.model_validate(findings)
    _replay_ocr_metrics(payload.get("ocr"), limits)
    return result


def _run_child(data: bytes, limits: ScanLimits, workdir: str) -> tuple[str, OcrFindings | None]:
    """Run one child to completion. Returns ``(outcome, findings)``."""
    with (
        open(os.path.join(workdir, "stdout"), "w+b") as out,
        open(os.path.join(workdir, "stderr"), "w+b") as err,
    ):
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and module, no shell
                _child_argv(limits),
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                env=_child_env(workdir),
                start_new_session=True,
            )
        except OSError as exc:
            _log.warning("risk_scan_spawn_failed", reason=str(exc))
            return "spawn_failed", None
        timed_out = False
        try:
            proc.communicate(input=data, timeout=limits.wall_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _kill_group(proc)
        events, reason, stderr_bytes = _child_said(err)
        if timed_out:
            _log.warning("risk_scan_timeout", wall_timeout=limits.wall_timeout, child_events=events)
            return "timeout", None
        if proc.returncode != 0:
            _log.warning(
                "risk_scan_failed",
                returncode=proc.returncode,
                reason=reason,
                child_events=events,
                stderr_bytes=stderr_bytes,
            )
            return "failed", None
        if events:
            _log.warning("risk_scan_degraded", child_events=events)
        out.seek(0)
        raw = out.read(limits.max_output_bytes + 1)
    try:
        return "ok", _parse(raw, limits)
    except Exception as exc:
        _log.warning("risk_scan_bad_result", reason=type(exc).__name__)
        return "bad_result", None


def scan_sandboxed(data: bytes, limits: ScanLimits | None = None) -> OcrFindings | None:
    """Risk-scan one image in a child process (blocking; run off-loop).

    Same contract as :func:`optimus.services.detection.riskscan.scan`: the
    findings, or ``None`` when there is nothing noteworthy or the scan could
    not be completed. Never raises.
    """
    lim = limits or ScanLimits()
    started = time.monotonic()
    try:
        # A private directory for the child's output and for the temporary
        # files pytesseract writes (downscaled copies of the image), removed
        # here whatever happens to the child.
        with tempfile.TemporaryDirectory(
            prefix="optimus-scan-", ignore_cleanup_errors=True
        ) as workdir:
            outcome, findings = _run_child(data, lim, workdir)
    except Exception as exc:
        _log.warning("risk_scan_error", reason=str(exc))
        outcome, findings = "error", None
    SCAN_CHILD_DURATION.observe(time.monotonic() - started)
    SCAN_CHILD_OUTCOME.labels(outcome=outcome).inc()
    return findings
