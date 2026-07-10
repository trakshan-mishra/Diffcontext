# Changelog

All notable changes to DiffContext are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/). Until 1.0.0, minor versions may
contain breaking changes; only symbols exported via `diffcontext.__all__` are
covered by any stability expectation.

## [Unreleased]

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
