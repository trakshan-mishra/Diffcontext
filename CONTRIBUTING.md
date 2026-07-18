# Contributing to DiffContext

## Setup

```bash
git clone https://github.com/trakshan-mishra/Diffcontext.git
cd Diffcontext
pip install -e .[dev]
python3 -m pytest tests/ -q      # 157 tests, self-contained, <3s
```

Optional extras: `pip install -e ".[typescript]"` for the TS/JS adapter
(tree-sitter). The benchmark scripts additionally need
`pip install rank-bm25`.

## Design constraints (please keep these)

- **The core package has zero runtime dependencies.** Everything under
  `diffcontext/` (except `languages/`) is stdlib-only, Python 3.8+. New
  dependencies belong in an optional extra or a separate tool, not in the
  core.
- **Optional language adapters stay optional.** `diffcontext/languages/`
  must import lazily; without the extra installed, behavior is identical
  to the Python-only tool.
- **The public API is the `__all__` list in `diffcontext/__init__.py`.**
  Everything else is internal and may change; don't grow the public
  surface casually.
- **Claims are measured, not asserted.** This project's convention is to
  state limitations as plainly as strengths (see
  [docs/BENCHMARKS.md](docs/BENCHMARKS.md)). If you add a capability,
  add a test that asserts the resolved edge/behavior — not just "it ran."
  If you find a limitation, document it rather than imply it away.

## Where things live

- `diffcontext/` — the package; module map in
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- `tests/` — fast, self-contained; run on every push across Python
  3.8–3.13
- `benchmarks/` — retrieval-quality evaluation; heavy runs are manual,
  but `benchmarks/check_regression.py` runs in CI
- `docs/` — architecture, benchmarks, verify methodology, language
  adapters, planning notes
- `diffcontext-service/` — optional FastAPI service + web UI; excluded
  from the wheel

## CI gates your PR must pass

1. **Tests** on Python 3.8, 3.10, 3.12, 3.13 (`pytest tests/`).
2. **Retrieval quality gate**: `benchmarks/check_regression.py` re-runs
   the co-change benchmark on flask and fails if hit/recall drop below
   frozen floors. If your change trades quality away on purpose, say so
   in the PR and adjust the floors in the same commit with the new
   measured numbers.
3. **Wheel hygiene**: the built wheel must contain only the package (no
   tests/benchmarks/service files) and must include `py.typed`.

## Adding a language adapter

`diffcontext/languages/` is the template — the TypeScript adapter
(tree-sitter based) is the reference implementation. An adapter provides
symbol extraction and edge resolution; the pipeline, scoring, and
compiler are language-agnostic. Before claiming support, measure it:
mine co-change cases with `diffcontext verify --from-history 25` on at
least a few real repos and report per-style results, including failure
modes ([docs/LANG_ADAPTERS.md](docs/LANG_ADAPTERS.md) shows the expected
reporting format).
