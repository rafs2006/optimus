"""Ratchet gate for file size and function size/complexity.

The rules (``docs/architecture.md#keeping-files-small``):

* A source file may not grow past its ceiling in ``structure-baseline.json``.
  Files with no ceiling (every new file) must stay at or under
  ``NEW_FILE_LIMIT`` lines.
* A function ruff flags as too complex (C901) or with too many statements
  (PLR0915) must already be in the baseline, and may not get worse.
* Ceilings only go down. ``--update`` lowers them to today's numbers and
  drops entries that no longer need one; it never raises or adds one.

Raising a ceiling is an explicit, reviewed act: run ``--update --allow-raise``
and commit the baseline. In a pull request, ``--base <ref>`` fails when the
baseline was raised, unless the PR carries the ``structure-exception`` label
(``STRUCTURE_EXCEPTION=true``). That keeps an urgent one-line fix in a big
file possible without making the cleanup a precondition.

Usage::

    uv run python scripts/check_structure.py            # check
    uv run python scripts/check_structure.py --update   # lower ceilings
    uv run python scripts/check_structure.py --base origin/main  # CI, PRs
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "structure-baseline.json"
SOURCES = ("src/optimus",)
NEW_FILE_LIMIT = 500
MAX_COMPLEXITY = 10
MAX_STATEMENTS = 50
EXCEPTION_LABEL = "structure-exception"

_COMPLEXITY = re.compile(r"too complex \((\d+) > \d+\)")
_STATEMENTS = re.compile(r"Too many statements \((\d+) > \d+\)")

Metrics = dict[str, dict[str, int]]
Baseline = dict[str, Any]


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def measure_files() -> dict[str, int]:
    out: dict[str, int] = {}
    for top in SOURCES:
        for path in sorted((ROOT / top).rglob("*.py")):
            out[path.relative_to(ROOT).as_posix()] = _line_count(path)
    return out


def _qualnames(path: Path) -> dict[int, str]:
    """``def`` line -> dotted qualified name, for every function in a file."""
    names: dict[int, str] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    lines = [child.lineno, *(d.lineno for d in child.decorator_list)]
                    for line in lines:
                        names[line] = name
                visit(child, f"{name}.")

    visit(ast.parse(path.read_text(encoding="utf-8")), "")
    return names


def measure_functions() -> Metrics:
    """Functions over the limits, as ``path::qualname -> {metric: value}``."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            *SOURCES,
            "--select",
            "C901,PLR0915",
            "--config",
            f"lint.mccabe.max-complexity={MAX_COMPLEXITY}",
            "--config",
            f"lint.pylint.max-statements={MAX_STATEMENTS}",
            "--output-format",
            "json",
            "--exit-zero",
            "--no-cache",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    out: Metrics = {}
    qual_cache: dict[str, dict[int, str]] = {}
    for item in json.loads(proc.stdout):
        rel = Path(item["filename"]).resolve().relative_to(ROOT).as_posix()
        quals = qual_cache.setdefault(rel, _qualnames(ROOT / rel))
        row = int(item["location"]["row"])
        name = quals.get(row, f"line{row}")
        if item["code"] == "C901" and (m := _COMPLEXITY.search(item["message"])):
            out.setdefault(f"{rel}::{name}", {})["complexity"] = int(m.group(1))
        elif item["code"] == "PLR0915" and (m := _STATEMENTS.search(item["message"])):
            out.setdefault(f"{rel}::{name}", {})["statements"] = int(m.group(1))
    return dict(sorted(out.items()))


def load(text: str | None = None) -> Baseline:
    data = json.loads(text if text is not None else BASELINE.read_text(encoding="utf-8"))
    return {"files": data.get("files", {}), "functions": data.get("functions", {})}


def violations(baseline: Baseline, files: dict[str, int], functions: Metrics) -> list[str]:
    problems: list[str] = []
    ceilings: dict[str, int] = baseline["files"]
    for path, lines in files.items():
        limit = ceilings.get(path, NEW_FILE_LIMIT)
        if lines > limit:
            kind = "ceiling" if path in ceilings else "limit for files without a ceiling"
            problems.append(f"{path}: {lines} lines, over its {kind} of {limit}")
    known: Metrics = baseline["functions"]
    for key, metrics in functions.items():
        allowed = known.get(key)
        if allowed is None:
            detail = ", ".join(f"{k} {v}" for k, v in sorted(metrics.items()))
            problems.append(
                f"{key}: over the limits ({detail}; max complexity {MAX_COMPLEXITY}, "
                f"max statements {MAX_STATEMENTS}). Split it."
            )
            continue
        for metric, value in metrics.items():
            if value > allowed.get(metric, 0):
                problems.append(
                    f"{key}: {metric} {value}, over its ceiling of {allowed.get(metric, 0)}"
                )
    return problems


def tightened(
    baseline: Baseline, files: dict[str, int], functions: Metrics, *, allow_raise: bool
) -> Baseline:
    new_files: dict[str, int] = {}
    for path, lines in files.items():
        old = baseline["files"].get(path)
        if lines <= NEW_FILE_LIMIT:
            continue  # back under the limit: no ceiling needed
        if allow_raise:
            new_files[path] = lines
        elif old is not None:
            new_files[path] = min(lines, old)
    new_funcs: Metrics = {}
    for key, metrics in functions.items():
        old_m = baseline["functions"].get(key)
        if old_m is None and not allow_raise:
            continue
        merged = {
            metric: (
                value if allow_raise or old_m is None else min(value, old_m.get(metric, value))
            )
            for metric, value in metrics.items()
        }
        new_funcs[key] = merged
    return {"files": dict(sorted(new_files.items())), "functions": dict(sorted(new_funcs.items()))}


def raised(base: Baseline, head: Baseline) -> list[str]:
    """Ceilings in ``head`` that are higher than, or missing from, ``base``."""
    out: list[str] = []
    for path, limit in head["files"].items():
        before = base["files"].get(path, NEW_FILE_LIMIT)
        if limit > before:
            out.append(f"{path}: file ceiling {before} -> {limit}")
    for key, metrics in head["functions"].items():
        before_m = base["functions"].get(key, {})
        for metric, value in metrics.items():
            if value > before_m.get(metric, 0):
                out.append(f"{key}: {metric} ceiling {before_m.get(metric, 0)} -> {value}")
    return out


def _write(data: Baseline) -> None:
    BASELINE.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--update", action="store_true", help="lower ceilings to today's numbers")
    ap.add_argument("--allow-raise", action="store_true", help="with --update: also raise/add")
    ap.add_argument("--base", help="git ref to compare the baseline with (pull requests)")
    args = ap.parse_args(argv)

    files, functions = measure_files(), measure_functions()
    baseline = load() if BASELINE.exists() else {"files": {}, "functions": {}}

    if args.update:
        _write(tightened(baseline, files, functions, allow_raise=args.allow_raise))
        print(f"wrote {BASELINE.name}")
        return 0

    exception = os.environ.get("STRUCTURE_EXCEPTION", "").lower() == "true"
    failed = False

    problems = violations(baseline, files, functions)
    for p in problems:
        print(f"::error::{p}")
    failed |= bool(problems)

    if args.base:
        shown = subprocess.run(
            ["git", "show", f"{args.base}:{BASELINE.name}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if shown.returncode == 0 and not BASELINE.exists():
            # Deleting (or renaming) the baseline would otherwise wave every
            # file through as "no ceilings yet".
            print(f"::error::{BASELINE.name} exists on {args.base} but was removed.")
            failed = True
        elif shown.returncode == 0:
            ups = raised(load(shown.stdout), baseline)
            level = "warning" if exception else "error"
            for up in ups:
                print(f"::{level}::baseline raised: {up}")
            if ups and not exception:
                print(f"Raising a ceiling needs the '{EXCEPTION_LABEL}' label on the PR.")
                failed = True

    slack = tightened(baseline, files, functions, allow_raise=False)
    if slack != baseline:
        print(
            "note: some code got smaller. Run "
            "`uv run python scripts/check_structure.py --update` to lock that in."
        )

    if failed:
        print(
            "\nStructure gate failed. Split the file or function by feature "
            "(docs/architecture.md#keeping-files-small). For an urgent fix that "
            "cannot wait for the split, run `--update --allow-raise`, commit the "
            f"baseline and add the '{EXCEPTION_LABEL}' label."
        )
        return 1
    print(f"structure ok: {len(files)} files, {len(functions)} functions over limits (all known)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
