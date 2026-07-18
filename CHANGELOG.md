# Changelog

All notable changes to DiffContext are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/). Until 1.0.0, minor versions may
contain breaking changes; only symbols exported via `diffcontext.__all__` are
covered by any stability expectation.

## [Unreleased]

### Added (experimental TypeScript/JavaScript support — `[typescript]` extra)
- New `diffcontext/languages/` adapter layer: the pipeline (scoring,
  selection, compilation, caching, diff mapping, verify) was already
  language-agnostic; an adapter supplies the two Python-bound pieces —
  per-file symbols and a dependency graph. Adapters are optional extras
  probed at import: without `pip install diffcontext[typescript]`, the
  tool is bit-for-bit the Python-only tool (asserted by the test suite,
  which skips the adapter tests when the extra is absent).
- The TypeScript/JavaScript adapter (tree-sitter) resolves functions,
  class methods, arrow consts, namespaces, ES imports with barrel
  `index.ts` re-export following (`export {X} from`, `export * from`,
  depth-capped), `this.method()`, `super()` → parent constructor,
  `new Class()` → constructor, `extends` override edges (child→parent,
  same mega-hub rationale as Python's), and function references passed
  as call arguments with parameter-shadowing guarded. Deliberately
  unresolved in v1 (disclosed in README): type inference, tsconfig
  path aliases, CommonJS `require()`.
- Integration is symmetric with Python: `.ts` files participate in the
  content-addressed state hash (a one-file edit invalidates the cached
  graph exactly like a `.py` edit), symbols go through the same SQLite
  symbol cache, `index.update()` handles changed/deleted TS files
  (adapter part rebuilt whole — cross-file barrel effects — and verified
  equal to a from-scratch rebuild by the test suite), and
  `verify --from-history` mines TS co-change cases through the adapter.
- Vendor-pollution guard, measured hazard: without an adapter-level
  exclusion policy (`static/`, `vendor/`, `*.min.*`, colocated
  `*.test.ts`/`*.spec.ts`), indexing django pulled in its tracked admin
  static JS — jquery included — 47 vendor symbols in a Python repo's
  index. Policy is adapter-scoped so a Python package named `static/`
  keeps being indexed.
- Declared-type resolution (second pass, after review): parameter /
  field / local annotations (`u: User`, `private db: Database`,
  constructor parameter properties) and `new X()` inference resolve
  `u.login()`, `this.db.query()`, `this.cache.close()` to the defining
  class method (following `extends` one level); tsconfig/jsconfig
  `baseUrl` + `paths` aliases resolve `@services/*`-style imports
  (JSONC-tolerant parser; `extends` chains not followed); and every
  interface/type-alias a signature mentions gets a consumer→type edge —
  the TS-specific co-change pattern (implementations change with the
  types they annotate with) that call scanning can never see. Graph
  density roughly doubled on all measured repos (hono 648→1,119 edges,
  zod 687→1,275, ky 144→181).
- Measured on FOUR real repos of different shapes, same
  `verify --from-history 25` harness (django's Python mined-case
  baseline: 58.6%): hono (ESM TS framework) 19/25, 67.9%; zod (TS
  monorepo, type-heavy) 16/25, 58.3%; ky (small ESM lib, mega-commit
  history) 6/25, 34.5%; express (CommonJS) 0/19, **0.0%**. The finding:
  retrieval quality tracks code STYLE, not language — ESM TS with a
  clear import graph lands in the Python band; CommonJS is a named,
  measured failure mode (`exports.x = function` yields almost no
  symbols). These are mined-case smoke signals, not the five-repo
  benchmark methodology.
- Known, prominently disclosed: the `verify` sufficiency score has ZERO
  discriminating power on TypeScript today (reported 100 on every hono
  case while measured recall ranged 50–100%) — its structural inputs
  were designed against Python graph density. The README carries a
  warning box; TS-aware sufficiency inputs are the top adapter roadmap
  item together with CommonJS support.

### Performance (cold index 3.5× — profile-driven, behavior-identical)
- Cold indexing of django (909 files, 9,161 symbols) dropped from ~23s to
  ~6.6s through four fixes, each verified behavior-identical on the full
  django corpus before landing:
  - Symbol code extraction split the ENTIRE file per symbol
    (`ast.get_source_segment` → 9,259 full-file line splits, 22% of cold
    runtime). Source is now split once per file and symbols sliced by
    line/col with UTF-8 byte offsets honored; files containing `\r`/`\f`
    fall back to `ast.get_source_segment`. Output byte-identical on all
    9,259 django symbols.
  - Function collection and import scanning walked every AST node;
    `def`/`class`/`import` are statements, so both now walk statement
    blocks only, skipping expression subtrees (~10:1 node ratio).
    Collected symbols and import maps identical on all 909 files.
  - `_follow_init_reexport` re-read and re-parsed `__init__.py` from disk
    on every consult — 1,224 redundant parses per django cold index
    (django.db.models alone is consulted by hundreds of files). Re-export
    specs are now parsed once and cached with mtime/size validation.
  - `SymbolCache.get_or_parse` re-read every file from disk to hash it
    when the pipeline had already hashed the same bytes for the repo
    state hash; callers now pass `known_hash`. The SQLite cache also
    pairs WAL with `synchronous=NORMAL` — contents are rebuildable, so
    the per-file-commit fsync bought nothing.
- Warm re-index of django: 0.11s. Retrieval quality gate re-run on
  django after the changes: hybrid hit 0.888 / recall 0.782 (floors
  0.80 / 0.68) — unchanged, as expected from behavior-identical fixes.

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
