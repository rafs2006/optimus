"""Tests for the sandboxed (child-process) OCR/QR risk scan.

The end-to-end tests start the real child. They use QR codes, which OpenCV
decodes on its own, so they do not depend on the tesseract binary: where it is
missing the OCR stage degrades to empty text, as it does in production.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2
import pytest
from prometheus_client import REGISTRY

import optimus
from optimus.contracts.events import OcrFindings
from optimus.services.detection import riskscan_sandbox as sandbox
from optimus.services.detection.riskscan_sandbox import ScanLimits, scan_sandboxed

LOOKALIKE = "https://perplexity-claim.com/pro"


def _qr_png(payload: str, *, scale: int = 8) -> bytes:
    code = cv2.QRCodeEncoder_create().encode(payload)
    code = cv2.copyMakeBorder(code, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
    code = cv2.resize(code, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", code)
    assert ok
    return bytes(buf.tobytes())


def _child_outcomes() -> dict[str, float]:
    names = ("ok", "timeout", "failed", "bad_result", "spawn_failed", "error")
    return {
        name: REGISTRY.get_sample_value("optimus_risk_scan_child_total", {"outcome": name}) or 0.0
        for name in names
    }


def _ocr_runs() -> float:
    return REGISTRY.get_sample_value("optimus_ocr_duration_seconds_count") or 0.0


def _ocr_outcome(outcome: str) -> float:
    return REGISTRY.get_sample_value("optimus_ocr_outcome_total", {"outcome": outcome}) or 0.0


def _delta(before: dict[str, float]) -> dict[str, float]:
    return {k: v - before[k] for k, v in _child_outcomes().items() if v != before[k]}


def _fake_child(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """Replace the real child with a one-line Python program."""
    monkeypatch.setattr(sandbox, "_child_argv", lambda _limits: [sys.executable, "-c", source])


# --- end to end, real child ---------------------------------------------------


def test_qr_lookalike_is_found_by_the_child() -> None:
    before, ocr_before = _child_outcomes(), _ocr_runs()
    findings = scan_sandboxed(_qr_png(LOOKALIKE))
    assert findings is not None
    assert findings.qr_urls == [LOOKALIKE]
    assert findings.risk_level in ("high", "critical")
    assert "lookalike_domain" in findings.signals
    assert _delta(before) == {"ok": 1.0}
    # The child's OCR run is replayed on this process's series.
    assert _ocr_runs() == ocr_before + 1


def test_undecodable_bytes_are_nothing_noteworthy() -> None:
    before = _child_outcomes()
    assert scan_sandboxed(b"not an image at all") is None
    assert _delta(before) == {"ok": 1.0}


def test_wall_timeout_kills_the_child_and_returns_none() -> None:
    before = _child_outcomes()
    started = time.monotonic()
    assert scan_sandboxed(_qr_png(LOOKALIKE), ScanLimits(wall_timeout=0.01)) is None
    assert time.monotonic() - started < 5
    assert _delta(before) == {"timeout": 1.0}


def test_memory_limit_too_small_to_start_is_a_failed_scan() -> None:
    before = _child_outcomes()
    limits = ScanLimits(mem_bytes=48 * 1024 * 1024)
    assert scan_sandboxed(_qr_png(LOOKALIKE), limits) is None
    assert _delta(before) == {"failed": 1.0}


def test_child_environment_has_no_settings_or_cloud_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPTIMUS_DISCORD_TOKEN", "should-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")
    probe = (
        "import json, os, sys; json.dump({'findings': {'risk_level': 'low', 'risk_score': 0,"
        " 'signals': sorted(k for k in os.environ if k.startswith(('OPTIMUS_', 'AWS_')))}},"
        " sys.stdout)"
    )
    _fake_child(monkeypatch, probe)
    findings = scan_sandboxed(b"x")
    assert findings is not None
    assert findings.signals == []


# --- what the child leaves behind ---------------------------------------------

_LEAVE_A_FILE = """
import json, os, sys, tempfile, time
left = os.path.join(tempfile.gettempdir(), "tess_leftover.png")
with open(left, "w") as handle:
    handle.write("a copy of the image")
with open(sys.argv[1], "w") as marker:
    marker.write(left)
if sys.argv[2] == "hang":
    time.sleep(60)
json.dump({"findings": None}, sys.stdout)
"""


@pytest.mark.parametrize(("mode", "outcome"), [("exit", "ok"), ("hang", "timeout")])
def test_child_temp_files_are_removed_even_when_it_is_killed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, outcome: str
) -> None:
    """pytesseract writes downscaled copies of the image to its temp directory.

    Killed in the middle of OCR, the child cannot clean up after itself, so the
    parent owns the directory.
    """
    marker = tmp_path / "marker"
    argv = [sys.executable, "-c", _LEAVE_A_FILE, str(marker), mode]
    monkeypatch.setattr(sandbox, "_child_argv", lambda _limits: argv)
    before = _child_outcomes()
    assert scan_sandboxed(b"img", ScanLimits(wall_timeout=3.0)) is None
    assert _delta(before) == {outcome: 1.0}
    left = Path(marker.read_text())
    assert left.parent.name.startswith("optimus-scan-")
    assert not left.parent.exists()


_LEAVE_A_PROCESS = """
import json, subprocess, sys
orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
json.dump({"findings": {"risk_level": "low", "risk_score": 0, "signals": [str(orphan.pid)]}},
          sys.stdout)
"""


def _is_running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc (Linux)")
def test_processes_the_child_leaves_running_are_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that exits normally can still have left Tesseract behind."""
    _fake_child(monkeypatch, _LEAVE_A_PROCESS)
    findings = scan_sandboxed(b"img")
    assert findings is not None
    orphan = int(findings.signals[0])
    # SIGKILL is already sent; give the kernel a moment to deliver it.
    deadline = time.monotonic() + 5
    while _is_running(orphan) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _is_running(orphan)


# --- memory: the reason this module exists ------------------------------------


def test_importing_the_sandbox_does_not_load_opencv() -> None:
    """The long-lived process must stay free of OpenCV and pytesseract."""
    code = (
        "import sys; import optimus.services.detection.riskscan_sandbox; "
        "print(sorted(m for m in ('cv2', 'pytesseract') if m in sys.modules))"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(optimus.__file__).resolve().parents[1]))
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
    assert out.stdout.strip() == "[]"


_GROWTH_PROBE = """
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from optimus.services.detection.riskscan_sandbox import scan_sandboxed

def rss_mb():
    pages = int(Path("/proc/self/statm").read_text().split()[1])
    return pages * os.sysconf("SC_PAGE_SIZE") / 2**20

data = Path(sys.argv[1]).read_bytes()
before = rss_mb()
with ThreadPoolExecutor(4) as pool:
    found = sum(r is not None for r in pool.map(scan_sandboxed, [data] * 4))
json.dump({"found": found, "growth_mb": rss_mb() - before, "opencv": "cv2" in sys.modules},
          sys.stdout)
"""


@pytest.mark.skipif(not Path("/proc/self/statm").exists(), reason="needs /proc (Linux)")
def test_scanning_large_images_does_not_grow_the_calling_process(tmp_path: Path) -> None:
    """Four concurrent scans of a 12 MP image, measured in a fresh process.

    A 12 MP image is ~36 MB decoded, before any preprocessing variant. Scanned
    on threads inside the process, this exact workload left ~75 MB resident
    after the scans had finished (and several hundred MB with mixed sizes); in
    child processes it leaves under 1 MB. The soak benchmark's 64x64 images
    could never show the difference, so this uses a realistic size. A fresh
    process keeps the baseline free of whatever earlier tests allocated.
    """
    from PIL import Image

    canvas = Image.new("RGB", (4000, 3000), (250, 250, 250))
    canvas.paste(Image.open(io.BytesIO(_qr_png(LOOKALIKE))), (200, 200))
    image = tmp_path / "large.jpg"
    canvas.save(image, format="JPEG", quality=85)

    env = dict(os.environ, PYTHONPATH=str(Path(optimus.__file__).resolve().parents[1]))
    out = subprocess.run(
        [sys.executable, "-c", _GROWTH_PROBE, str(image)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    report = json.loads(out.stdout)
    assert report["found"] == 4
    assert report["opencv"] is False
    assert report["growth_mb"] < 16


# --- child failures -----------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "outcome"),
    [
        ("import sys; sys.exit(3)", "failed"),
        ("print('not json')", "bad_result"),
        ("print('[1, 2]')", "bad_result"),
        ("print('x' * 200_000)", "bad_result"),
        ("import json; print(json.dumps({'findings': {'risk_level': 'high'}}))", "bad_result"),
    ],
)
def test_child_misbehaviour_is_nothing_noteworthy(
    monkeypatch: pytest.MonkeyPatch, source: str, outcome: str
) -> None:
    _fake_child(monkeypatch, source)
    before = _child_outcomes()
    assert scan_sandboxed(b"img") is None
    assert _delta(before) == {outcome: 1.0}


def test_spawn_failure_is_nothing_noteworthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_child_argv", lambda _limits: ["/nonexistent/python"])
    before = _child_outcomes()
    assert scan_sandboxed(b"img") is None
    assert _delta(before) == {"spawn_failed": 1.0}


def test_unexpected_error_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_data: bytes, _limits: ScanLimits, _workdir: str) -> tuple[str, OcrFindings | None]:
        raise RuntimeError("disk full")

    monkeypatch.setattr(sandbox, "_run_child", boom)
    before = _child_outcomes()
    assert scan_sandboxed(b"img") is None
    assert _delta(before) == {"error": 1.0}


def test_unwritable_temp_directory_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox.tempfile, "tempdir", str(tmp_path / "missing"))
    before = _child_outcomes()
    assert scan_sandboxed(b"img") is None
    assert _delta(before) == {"error": 1.0}


def test_child_that_exits_early_without_reading_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_child(monkeypatch, "import json, sys; json.dump({'findings': None}, sys.stdout)")
    assert scan_sandboxed(b"x" * 5_000_000) is None


# --- result parsing and metric replay -----------------------------------------


def _result(findings: object = None, ocr: object = None) -> bytes:
    return json.dumps({"findings": findings, "ocr": ocr}).encode()


def test_parse_returns_validated_findings() -> None:
    raw = _result({"risk_level": "critical", "risk_score": 9, "signals": ["wallet"]})
    findings = sandbox._parse(raw, ScanLimits())
    assert findings == OcrFindings(risk_level="critical", risk_score=9, signals=["wallet"])


def test_parse_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra"):
        sandbox._parse(_result({"risk_level": "high", "risk_score": 1, "extra": 1}), ScanLimits())


def test_parse_replays_a_sane_ocr_report() -> None:
    runs, exhausted = _ocr_runs(), _ocr_outcome("budget_exhausted")
    report = {"seconds": 8.0, "variants": 2, "outcome": "budget_exhausted"}
    assert sandbox._parse(_result(ocr=report), ScanLimits()) is None
    assert _ocr_runs() == runs + 1
    assert _ocr_outcome("budget_exhausted") == exhausted + 1


@pytest.mark.parametrize(
    "report",
    [
        None,
        "complete",
        {"seconds": 1.0, "variants": 1},
        {"seconds": 1.0, "variants": 1, "outcome": "made_up_label"},
        {"seconds": -1.0, "variants": 1, "outcome": "complete"},
        {"seconds": float("nan"), "variants": 1, "outcome": "complete"},
        {"seconds": 1e9, "variants": 1, "outcome": "complete"},
        {"seconds": True, "variants": 1, "outcome": "complete"},
        {"seconds": 1.0, "variants": 9, "outcome": "complete"},
        {"seconds": 1.0, "variants": 1.5, "outcome": "complete"},
        {"seconds": 1.0, "variants": True, "outcome": "complete"},
        {"seconds": 1.0, "variants": 1, "outcome": ["complete"]},
        {"seconds": 10**400, "variants": 1, "outcome": "complete"},
        {"seconds": float("inf"), "variants": 1, "outcome": "complete"},
    ],
)
def test_implausible_ocr_report_is_not_recorded(report: object) -> None:
    """Dropped without raising: a bad report must not cost the findings."""
    runs = _ocr_runs()
    sandbox._replay_ocr_metrics(report, ScanLimits())
    assert _ocr_runs() == runs
    assert _ocr_outcome("made_up_label") == 0


# --- child environment and limits ---------------------------------------------


def test_child_env_drops_settings_and_pins_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTIMUS_DISCORD_TOKEN", "should-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")
    monkeypatch.setenv("TESSDATA_PREFIX", "/usr/share/tessdata")
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    env = sandbox._child_env("/scratch/scan")
    assert not [k for k in env if k.startswith(("OPTIMUS_", "AWS_"))]
    assert env["TESSDATA_PREFIX"] == "/usr/share/tessdata"
    assert env["TMPDIR"] == "/scratch/scan"
    assert env["OMP_THREAD_LIMIT"] == "1"
    assert env["MALLOC_ARENA_MAX"] == "2"
    root = str(Path(optimus.__file__).resolve().parents[1])
    assert env["PYTHONPATH"] == f"{root}{os.pathsep}/somewhere/else"


def test_child_env_sets_pythonpath_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    root = str(Path(optimus.__file__).resolve().parents[1])
    assert sandbox._child_env("/scratch/scan")["PYTHONPATH"] == root


def test_limits_follow_the_ocr_budget() -> None:
    limits = ScanLimits.for_ocr_timeout(3.0)
    assert limits.ocr_timeout == 3.0
    assert limits.wall_timeout == 33.0
    argv = sandbox._child_argv(limits)
    assert argv[:3] == [sys.executable, "-m", "optimus.services.detection.riskscan_child"]
    config = json.loads(argv[3])
    assert config["ocr_timeout"] == 3.0
    assert config["cpu_seconds"] == 33
    assert config["mem_bytes"] == limits.mem_bytes
    assert config["file_bytes"] == limits.file_bytes


def test_default_wall_clock_tracks_the_default_ocr_budget() -> None:
    """The sandbox cannot import the OCR module's constant; keep the copy honest."""
    from optimus.hashing import ocr_extract

    assert sandbox._DEFAULT_OCR_SECONDS == ocr_extract._OCR_TOTAL_TIMEOUT_SECONDS
    assert ScanLimits().wall_timeout == ScanLimits.for_ocr_timeout(8.0).wall_timeout
