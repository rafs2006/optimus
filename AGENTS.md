# AGENTS.md

Instructions for coding agents working in this repository. Setup, the checks CI
runs and the test conventions are in [`CONTRIBUTING.md`](CONTRIBUTING.md). How
the services fit together and which module may import which is in
[`docs/architecture.md`](docs/architecture.md). Read both before changing code.

## Checks before you push

Run what CI runs ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)), in
this order:

```bash
uv sync --extra dev --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run python scripts/check_structure.py --base origin/main
uv run pytest
```

A check you could not run is reported as not run, never as passing.

## File size: a ratchet, and split by domain

`scripts/check_structure.py` runs in CI and checks every hand-written Python
file against [`structure-baseline.json`](structure-baseline.json). That means
application code, tests, scripts and benchmarks: every `*.py` file git knows
about. Alembic revisions (`migrations/versions/`) are left out, because an
applied migration is never edited.

- A file listed in the baseline may not grow past its ceiling.
- Any other file, which means every new one, stays at or under 500 lines.
- Ceilings only go down. `--update` lowers them to today's numbers and drops a
  file that is back at or under 500 lines. It never raises or adds a ceiling.
- The same baseline holds ceilings for the functions in `src/optimus` that are
  too complex (ruff C901, over 10) or too long (PLR0915, over 50 statements).
  A new one fails, and a listed one may not get worse.

When a file reaches its ceiling, split it by domain: one concern per module,
placed next to the code it belongs with. `interactions/review_buttons.py`, one
function per review button moved out of `handlers.py`, is the model. A test
file is split the same way and follows the module it tests.

Getting under the number without separating a concern is not a split. Do not:

- pack several statements onto one line, or strip comments and blank lines;
- cut a file at an arbitrary point into `part2`, `more` or a catch-all `utils`;
- move code into a directory the gate does not measure.

**Urgent fix in a big file.** A fix does not wait for a refactor. Run
`uv run python scripts/check_structure.py --update --allow-raise`, commit the
baseline and say so in the PR. CI fails a PR that raises or adds a ceiling
unless the PR carries the `structure-exception` label. With the label the raise
is reported as a warning and stays visible in the diff.

The full rules are in
[`docs/architecture.md`](docs/architecture.md#keeping-files-small).
