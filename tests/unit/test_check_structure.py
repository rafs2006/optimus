"""The structure gate: ceilings only go down, new code stays small."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_structure", ROOT / "scripts/check_structure.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _gate()
LIMIT = gate.NEW_FILE_LIMIT


def _base(files: dict[str, int] | None = None, functions: dict | None = None) -> dict:  # type: ignore[type-arg]
    return {"files": files or {}, "functions": functions or {}}


def test_a_file_may_not_grow_past_its_ceiling() -> None:
    base = _base({"big.py": 900})
    assert gate.violations(base, {"big.py": 900}, {}) == []
    assert gate.violations(base, {"big.py": 901}, {}) != []


def test_a_file_without_a_ceiling_stays_under_the_limit() -> None:
    assert gate.violations(_base(), {"new.py": LIMIT}, {}) == []
    (problem,) = gate.violations(_base(), {"new.py": LIMIT + 1}, {})
    assert "without a ceiling" in problem


def test_a_new_long_or_complex_function_fails() -> None:
    (problem,) = gate.violations(_base(), {}, {"a.py::f": {"complexity": 11}})
    assert "Split it" in problem


def test_a_known_function_may_not_get_worse() -> None:
    base = _base(functions={"a.py::f": {"complexity": 20, "statements": 60}})
    assert gate.violations(base, {}, {"a.py::f": {"complexity": 19}}) == []
    assert gate.violations(base, {}, {"a.py::f": {"statements": 61}}) != []


def test_update_only_lowers_and_drops() -> None:
    base = _base({"big.py": 900, "fixed.py": 700}, {"a.py::f": {"complexity": 20}})
    files = {"big.py": 950, "fixed.py": 400, "new.py": 800}
    out = gate.tightened(
        base,
        files,
        {"a.py::f": {"complexity": 15}, "b.py::g": {"complexity": 30}},
        allow_raise=False,
    )
    assert out["files"] == {"big.py": 900}  # never raised, never added; fixed.py dropped
    assert out["functions"] == {"a.py::f": {"complexity": 15}}


def test_allow_raise_records_today() -> None:
    out = gate.tightened(_base({"big.py": 900}), {"big.py": 950}, {}, allow_raise=True)
    assert out["files"] == {"big.py": 950}


def test_raised_lists_every_raise_or_addition() -> None:
    before = _base({"big.py": 900}, {"a.py::f": {"complexity": 20}})
    after = _base(
        {"big.py": 901, "new.py": 600},
        {"a.py::f": {"complexity": 21}, "b.py::g": {"statements": 55}},
    )
    assert len(gate.raised(before, after)) == 4
    assert gate.raised(after, before) == []


def test_the_repo_passes_its_own_gate() -> None:
    assert gate.main([]) == 0


def test_removing_the_baseline_fails_against_a_base_that_has_it(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    import subprocess as sp

    monkeypatch.setattr(gate, "BASELINE", tmp_path / "structure-baseline.json")

    class _Shown:
        returncode = 0
        stdout = '{"files": {}, "functions": {}}'

    real_run = sp.run

    def fake_run(cmd: list[str], *a: Any, **kw: Any) -> Any:
        if cmd[:2] == ["git", "show"]:
            return _Shown()
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    assert gate.main(["--base", "origin/main"]) == 1
    assert "was removed" in capsys.readouterr().out
