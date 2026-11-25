"""The structure gate: ceilings only go down, new code stays small."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
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


def test_tests_scripts_and_benchmarks_count_but_applied_migrations_do_not() -> None:
    for path in ("src/optimus/a.py", "tests/unit/test_a.py", "scripts/a.py", "benchmarks/a.py"):
        assert gate.size_checked(path), path
    # An applied migration is never edited, so it cannot be split. The Alembic
    # runner next to the revisions is ordinary code.
    assert not gate.size_checked("migrations/versions/0001_initial_schema.py")
    assert gate.size_checked("migrations/env.py")
    for path in ("uv.lock", "docs/moderator-guide.md", "tests/fixtures/labels.json"):
        assert not gate.size_checked(path), path


def test_a_long_test_file_is_held_to_the_same_limit() -> None:
    (problem,) = gate.violations(_base(), {"tests/unit/test_new.py": LIMIT + 1}, {})
    assert "without a ceiling" in problem
    base = _base({"tests/unit/test_old.py": 900})
    assert gate.violations(base, {"tests/unit/test_old.py": 900}, {}) == []
    assert gate.violations(base, {"tests/unit/test_old.py": 901}, {}) != []


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_files_are_measured_from_what_git_knows(tmp_path: Path, monkeypatch: Any) -> None:
    # Inside a git hook or `rebase -x`, GIT_DIR and friends point at the outer
    # repo, and a user-wide ignore file could hide the files written below.
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    _git(tmp_path, "init", "-q")
    written = {
        "src/optimus/tracked.py": "a = 1\nb = 2\n",
        "tests/unit/test_tracked.py": "x = 1\n",
        "tests/unit/test_removed.py": "x = 1\n",
        "migrations/versions/0001_first.py": "x = 1\n",
        "migrations/env.py": "x = 1\n",
        "docs/notes.md": "text\n",
        ".venv/lib/vendored.py": "x = 1\n",
        ".gitignore": ".venv/\n",
    }
    for name, text in written.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    # Deleted but still in the index, and brand new but not yet added.
    (tmp_path / "tests/unit/test_removed.py").unlink()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/new_tool.py").write_text("x = 1\ny = 2\nz = 3", encoding="utf-8")

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    assert gate.measure_files() == {
        "migrations/env.py": 1,
        "scripts/new_tool.py": 3,
        "src/optimus/tracked.py": 2,
        "tests/unit/test_tracked.py": 1,
    }


def test_the_repo_measures_its_tests_and_every_big_file_has_a_ceiling() -> None:
    sizes = gate.measure_files()
    assert "tests/unit/test_check_structure.py" in sizes
    assert "scripts/check_structure.py" in sizes
    assert "migrations/env.py" in sizes
    assert not any(path.startswith("migrations/versions/") for path in sizes)
    ceilings = gate.load()["files"]
    assert {path for path, lines in sizes.items() if lines > LIMIT} <= set(ceilings)


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


ADDED = "tests/unit/test_check_structure.py"


def _head_with_one_added_ceiling(tmp_path: Path, monkeypatch: Any) -> None:
    """Give the head one ceiling, for a file in ``tests``, that the base lacks."""
    base = gate.load()
    assert ADDED not in base["files"]
    head = {"files": {**base["files"], ADDED: LIMIT + 100}, "functions": base["functions"]}
    head_file = tmp_path / gate.BASELINE.name
    head_file.write_text(json.dumps(head), encoding="utf-8")
    monkeypatch.setattr(gate, "BASELINE", head_file)

    class _Shown:
        returncode = 0
        stdout = json.dumps(base)

    real_run = subprocess.run

    def fake_run(cmd: list[str], *a: Any, **kw: Any) -> Any:
        if cmd[:2] == ["git", "show"]:
            return _Shown()
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(gate.subprocess, "run", fake_run)


def test_adding_a_ceiling_fails_without_the_label(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    _head_with_one_added_ceiling(tmp_path, monkeypatch)
    monkeypatch.delenv("STRUCTURE_EXCEPTION", raising=False)
    assert gate.main(["--base", "origin/main"]) == 1
    out = capsys.readouterr().out
    assert f"::error::baseline raised: {ADDED}: file ceiling {LIMIT} -> {LIMIT + 100}" in out
    assert f"needs the '{gate.EXCEPTION_LABEL}' label" in out


def test_adding_a_ceiling_is_a_warning_with_the_label(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    _head_with_one_added_ceiling(tmp_path, monkeypatch)
    monkeypatch.setenv("STRUCTURE_EXCEPTION", "true")
    assert gate.main(["--base", "origin/main"]) == 0
    assert f"::warning::baseline raised: {ADDED}" in capsys.readouterr().out
