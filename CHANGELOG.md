# Changelog

All notable changes to DiffContext are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/). Until 1.0.0, minor versions may
contain breaking changes; only symbols exported via `diffcontext.__all__` are
covered by any stability expectation.

## [Unreleased]

### Fixed (token budget now bounds the full output — follow-up to the earlier accounting fix)
- The earlier "honest token accounting" fix (below) made the meta-header
  budget-proportionate and made `token_estimate` report the full output —
  but it did not fix the selector/compiler mismatch in the code section
  itself: `select_context()` still budgeted on `token_count(symbol.code)`
  alone, while `compile_context()` rendered each selected symbol with a
  `FILE:`/`FUNCTION:` header and a CALLERS/CALLEES relationship block on
  top of the code. The gap reproduced on psf/black at every budget from
  500 to 8000 as a systematic 25-41% overshoot of `--max-tokens` (e.g.
  8000 requested → 11,268 emitted).
- The per-symbol rendering is now a shared function
  (`compiler.render_symbol_block`); the selector measures each candidate
  at its full rendered size (using a pessimistic empty selected-set so
  every relationship entry counts its longer `[NOT IN CONTEXT]` form),
  and the compiler enforces the budget against the final full output with
  a post-render trim pass that drops the lowest-scored non-changed
  symbols (disclosed in the DROPPED manifest) until the output fits.
  Same black sweep now (black @ `51abf530`, and including the structural-
  ceiling disclosure line added in the same release, which costs ~60 meta
  tokens at every budget): 1000→992, 2000→1,743, 4000→3,660, 8000→7,329.
- Remaining bounded exception, on purpose: the meta-header and the changed
  symbols themselves are never dropped (disclosure and the diff are the
  point of the output), so when that floor alone exceeds the requested
  budget the floor is emitted — e.g. the black case at `--max-tokens 500`
  emits 774 tokens (416 meta + 358 changed-symbol block). The overshoot is
  visible in the meta's own token lines, never silent.
  Regression-tested by `tests/test_token_budget.py`, which fails against
  the previous behavior.
- Library callers of `select_context()` that don't pass the new optional
  `graph` argument keep the old code-only accounting (and its overshoot);
  `pipeline.compile()` passes the graph, so the CLI and public API get the
  corrected behavior.

### Fixed (honest token accounting under tight budgets)
- `ContextPackage.token_estimate` now reports the FULL output (meta-header +
  relationship annotations + code + suggestions) — the number an agent
  harness actually pays — instead of the code sections only. Stress testing
  on psf/black (648 symbols) showed `--max-tokens 500` silently emitting
  ~2,600 tokens because the meta scaled with repo size, not with the budget.
- The meta-header is now budget-proportionate: under tight budgets the
  per-module architecture snapshot compacts to a summary line, the dropped
  manifest shows 5 entries instead of 15, and per-symbol CALLERS/CALLEES
  annotations cap at 3. Disclosure is never sacrificed — the full dropped
  COUNT and blind-spot count remain in every output. Same black case now
  emits ~1,390 tokens (was ~2,600); `compile(max_tokens=...)` is threaded
  through to the compiler to enable this.
- The meta line `Context tokens (est.)` was replaced by two lines:
  `Context tokens (code)` and `Output tokens (full)`, so the overhead is
  visible in the output itself.

### Fixed (resolver — src-layout and module-attribute calls)
- **Absolute imports now resolve under `src/` layouts.** Previously,
  absolute imports were resolved only against the repository root, so for
  the standard setuptools src-layout (used by black, flask, and most
  modern PyPI projects) *every* import of the project's own package
  silently failed — e.g. on psf/black, the import map for
  `src/blackd/__init__.py` contained a single entry and the call graph
  had no `blackd → black` edges at all, truncating blast radii one hop
  before the code under change. Absolute imports are now resolved against
  the repo root and `src/`, in that order. (Found by running the tool on
  psf/black; regression-tested by `tests/test_src_layout.py` on a
  src-layout fixture.)
- `import a.b` no longer binds the local name `a` to `a/b.py` (which sent
  `a.other()` calls into the wrong file); `a` now binds to package `a`,
  and the full dotted name `a.b` is bound as well so `a.b.fn()` resolves.
- Module-attribute calls through package re-exports now resolve:
  `black.parse_ast(...)` finds the definition in `black/parsing.py` via
  the `__init__.py`'s own import map (no extra parsing). `_follow_init_
  reexport` also follows absolute re-exports (`from black.parsing import
  X`), not just relative ones.

### Added (graph — function references as arguments)
- Function references passed as call arguments now create graph edges:
  `partial(black.format_file_contents, ...)`, `sorted(xs, key=fn)`,
  `map(fn, xs)`. On psf/black this is the only way `blackd`'s request
  handler references the formatting entry point — a call-only graph
  missed the edge entirely. Parameter and typed-local names shadow
  module-level functions and are skipped, so passing a parameter along
  never fabricates an edge to a same-named function.

### Hybrid retrieval (benchmark-driven; changes default ranking)
- `analyze_impact(hybrid=True)` is now the default: scores blend call-graph
  impact (0.5), BM25 lexical similarity (0.35), and same-file co-location
  (0.15) — the configuration that won recall on 4/5 repos in the eval_v2
  benchmark (django recall 0.660 → 0.782). `hybrid=False` (CLI
  `--graph-only`) restores the pure graph signal.
- New `diffcontext/lexical.py`: dependency-free BM25 over symbol source
  (inverted index; rank_bm25-compatible scoring with a positive idf-floor
  fix for tiny corpora). Cached per `RepositoryIndex`, invalidated by
  `update()`.
- `select_context(top_k=...)` / `compile(top_k=...)` / CLI `--top-k`
  (default 20 per changed symbol): caps retrieved symbols at the
  benchmarked recall/precision sweet spot.
- New hardened benchmark `benchmarks/eval_v2_hardened.py` (423 distinct
  commits, 5 repos, 4 baselines, budget sweep, failure taxonomy) with
  report at `benchmarks/EVAL_V2_REPORT.md`, plus
  `benchmarks/check_regression.py` and a CI `retrieval-quality-gate` job
  enforcing frozen quality floors on every push.

### Removed
- CtxSync cloud sync (`diffcontext sync`, `compile --sync`): the server
  side was never implemented and the integration required unavailable
  credentials. Legacy harnesses `run_benchmark.py` / `run_metrics.py`
  removed (superseded by `benchmarks/eval_v1.py` and eval_v2).

### Harness-facing API
- `ContextPackage.items`: structured base representation of a compiled
  context — a list of `ContextItem {symbol_id, code, score, role, callers,
  callees, token_estimate}` a harness can filter/reorder/re-budget itself.
  The formatted `text` is now a renderer over these items.
- Pluggable tokenizer: `select_context`, `pipeline.compile`, and the
  top-level `compile_context` accept `token_counter: Callable[[str], int]`
  so budgets can be enforced with a model's real tokenizer instead of the
  ~4-chars/token heuristic (still the default).
- `ScoringConfig` dataclass: impact-scoring weights are now a per-call
  parameter (`compute_impact_scores(config=...)`, `analyze_impact(
  scoring_config=...)`) instead of edit-the-source module constants;
  benchmark ablations can sweep configs directly.
- Warning de-duplication is now scoped per indexing session (`WarnState`)
  instead of process-global, so a long-lived process serving many repos
  warns correctly per session. `SymbolCache` is now safe for concurrent
  use from multiple threads in one process.

### Fixed (graph determinism)
- Sliding-window, same-directory, and shared-import edges were built from
  `set` iteration order, making parts of the graph vary with hash seed and
  insertion order. They now follow true definition order (sorted by line
  number), so graphs are deterministic across runs — required for
  reproducible benchmark results. Edge sets may differ slightly from
  previous releases.

### Performance
- Each Python file is now read and parsed exactly once per indexing run
  (previously up to 3×: symbol extraction, graph pre-pass, import maps).
- The call graph is persisted content-addressed (keyed by the combined hash
  of every file), so re-indexing an unchanged repo skips parsing and graph
  construction entirely. Measured on pydantic@652a61c (405 files, ~1,830
  symbols): warm re-index 1.62s → 0.024s.
- New `RepositoryIndex.update(changed_files)`: in-process incremental
  re-index that re-reads/re-parses only the changed files. Measured on
  pydantic: 0.56s per single-file update vs 1.62s full re-index. Verified
  equal (symbols + graph) to a from-scratch rebuild, in tests and on
  pydantic itself.

### Fixed
- The context meta-header's "Scoring basis" line is now derived from the
  live constants in `impact/scoring.py`; it previously hardcoded stale
  values (claimed direct_caller=80, actual 85; claimed 2-hop 60/50, actual
  58.5/72.2; described a structural-bonus formula that no longer exists).
- Version is now single-sourced from `diffcontext.__version__` (previously
  `pyproject.toml` said 0.1.0 while the package reported 0.2.0).
- Added the `py.typed` marker file that `pyproject.toml` already declared, so
  type checkers actually see the package's inline types.

### Added
- `LICENSE` file (MIT — previously claimed in README only).
- `__all__` in `diffcontext/__init__.py` marking the public, semver-covered API
  boundary: `blast_radius`, `index`, `diff`, `compile_context`, `BlastResult`.
- GitHub Actions CI (test on push/PR) and tag-triggered PyPI publishing via
  trusted publishing (OIDC).
- This changelog.

### Removed
- `mcp` optional extra and the `diffcontext-mcp` entry point (the entry point
  referenced a module that does not exist; MCP support is deferred).

## [0.2.0] — pre-changelog

Everything before this changelog existed: initial library API (`index`, `diff`,
`blast_radius`, `compile_context`), CLI, symbol cache, call-graph builder,
impact scoring, context compiler, eval_v1 benchmark suite.
