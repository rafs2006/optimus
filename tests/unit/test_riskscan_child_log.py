"""Tests for what the risk-scan parent repeats from its child's stderr.

The child's stderr can hold tracebacks that quote text read out of an image,
so the parent takes only event names and the one fixed-format failure line
from it. These tests pin that filter, and that the child's real log format and
the parent's reader agree.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from optimus.services.detection import riskscan_sandbox as sandbox
from optimus.services.detection.riskscan_sandbox import ScanLimits, scan_sandboxed


def _fake_child(monkeypatch: pytest.MonkeyPatch, source: str, *args: str) -> None:
    """Replace the real child with a small Python program."""
    argv = [sys.executable, "-c", source, *args]
    monkeypatch.setattr(sandbox, "_child_argv", lambda _limits: argv)


# --- the filter ---------------------------------------------------------------


def test_only_event_names_and_the_failure_line_are_taken_from_stderr() -> None:
    lines = [
        json.dumps({"level": "info", "message": "ocr_started"}),
        json.dumps(
            {"level": "warning", "message": "qr_extract_failed", "exception": "Traceback: text"}
        ),
        json.dumps({"level": "warning", "message": "qr_extract_failed"}),
        json.dumps({"level": "critical", "message": "ocr_unavailable"}),
        json.dumps({"level": "error", "message": "Claim your free reward at example.test"}),
        json.dumps({"level": "error", "message": ["not", "a", "name"]}),
        json.dumps(["warning", "not_an_object"]),
        "Traceback (most recent call last):",
        "risk scan failed: MemoryError: ",
        "risk scan failed: ImportError: failed to map segment",
    ]
    raw = "\n".join(lines).encode()
    events, reason, size = sandbox._child_said(io.BytesIO(raw))
    assert events == ["qr_extract_failed", "ocr_unavailable"]
    assert reason == "ImportError: failed to map segment"
    assert size == len(raw)


def test_nothing_is_taken_from_an_empty_or_binary_stderr() -> None:
    assert sandbox._child_said(io.BytesIO(b"")) == ([], None, 0)
    assert sandbox._child_said(io.BytesIO(bytes(range(256)) * 100)) == ([], None, 25600)


def test_a_line_cut_off_by_the_read_window_is_not_read_as_a_line() -> None:
    """Only the end of a long stderr is read; what it starts with is a fragment.

    Here the window opens exactly on text inside a longer line that happens to
    look like the failure line.
    """
    fragment = b"risk scan failed: quoted inside a longer line\n"
    window = fragment + b"z" * (sandbox._STDERR_READ_BYTES - len(fragment))
    raw = b'{"level": "info", "message": "' + window
    assert sandbox._child_said(io.BytesIO(raw)) == ([], None, len(raw))
    # The same text as a whole line of its own is the failure line.
    assert sandbox._child_said(io.BytesIO(fragment))[1] == "quoted inside a longer line"


# --- what reaches the service log ---------------------------------------------

_WARN_AND_ANSWER = """
import json, sys
print(json.dumps({"level": "warning", "message": "qr_extract_failed",
                  "exception": "Traceback: words read from the image"}), file=sys.stderr)
json.dump({"findings": None}, sys.stdout)
"""


def test_a_stage_that_failed_inside_the_child_is_logged_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan still answers, but the service log says a stage did not run."""
    _fake_child(monkeypatch, _WARN_AND_ANSWER)
    with capture_logs() as logs:
        assert scan_sandboxed(b"img") is None
    assert logs == [
        {
            "event": "risk_scan_degraded",
            "log_level": "warning",
            "child_events": ["qr_extract_failed"],
        }
    ]


_WARN_AND_HANG = """
import json, sys, time
print(json.dumps({"level": "warning", "message": "ocr_variant_failed"}), file=sys.stderr,
      flush=True)
with open(sys.argv[1], "w") as marker:
    marker.write("logged")
time.sleep(60)
"""


def test_a_killed_child_is_logged_with_what_it_had_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "marker"
    _fake_child(monkeypatch, _WARN_AND_HANG, str(marker))
    with capture_logs() as logs:
        assert scan_sandboxed(b"img", ScanLimits(wall_timeout=3.0)) is None
    assert marker.read_text() == "logged"
    assert logs == [
        {
            "event": "risk_scan_timeout",
            "log_level": "warning",
            "wall_timeout": 3.0,
            "child_events": ["ocr_variant_failed"],
        }
    ]


_FAIL_WITH_A_REASON = """
import json, sys
print(json.dumps({"level": "error", "message": "ocr_unavailable"}), file=sys.stderr)
print("Traceback (most recent call last): words read from the image", file=sys.stderr)
print("risk scan failed: OSError: no space left", file=sys.stderr)
sys.exit(1)
"""


def test_a_failed_child_is_logged_with_its_reason_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_child(monkeypatch, _FAIL_WITH_A_REASON)
    with capture_logs() as logs:
        assert scan_sandboxed(b"img") is None
    assert len(logs) == 1
    assert logs[0].pop("stderr_bytes") > 100
    assert logs == [
        {
            "event": "risk_scan_failed",
            "log_level": "warning",
            "returncode": 1,
            "reason": "OSError: no space left",
            "child_events": ["ocr_unavailable"],
        }
    ]


# --- the real child -----------------------------------------------------------

_REAL_MAIN_WITH_A_FAILING_STAGE = """
import sys
from pathlib import Path
from optimus.core.logging import get_logger
from optimus.services.detection import riskscan_child


def run(image_bytes, *, ocr_timeout):
    try:
        raise ValueError("words read from the image")
    except ValueError:
        get_logger("stage").warning("qr_extract_failed", exc_info=True)
    return {"findings": None, "ocr": None}


riskscan_child.run = run
raise SystemExit(riskscan_child.main(["riskscan_child", sys.argv[1]]))
"""


def test_warning_logged_through_the_real_child_reaches_the_service_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child's log format and the parent's reader have to agree.

    Runs the child's real ``main`` (limits, stdout handling, logging setup)
    around a scan stage that fails the way ``qr_extract`` reports it.
    """
    config = sandbox._child_argv(ScanLimits())[3]
    _fake_child(monkeypatch, _REAL_MAIN_WITH_A_FAILING_STAGE, config)
    with capture_logs() as logs:
        assert scan_sandboxed(b"img") is None
    assert logs == [
        {
            "event": "risk_scan_degraded",
            "log_level": "warning",
            "child_events": ["qr_extract_failed"],
        }
    ]


def test_real_child_names_its_failure_for_the_log() -> None:
    """A memory limit too small for the imports: the reason line, no traceback."""
    proc = subprocess.run(
        sandbox._child_argv(ScanLimits(mem_bytes=48 * 1024 * 1024)),
        input=b"img",
        capture_output=True,
        env=sandbox._child_env("/tmp"),
        check=False,
    )
    assert proc.returncode == 1
    events, reason, size = sandbox._child_said(io.BytesIO(proc.stderr))
    assert events == []
    assert reason is not None
    assert reason.startswith(("MemoryError", "ImportError", "OSError"))
    assert size == len(proc.stderr)
