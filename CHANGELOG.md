# Changelog

All notable changes to DiffContext are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/). Until 1.0.0, minor versions may
contain breaking changes; only symbols exported via `diffcontext.__all__` are
covered by any stability expectation.

## [Unreleased]

### Added (retrieval-quality bottleneck pass — roadmap items 1–3 shipped)
- **Dispatch-sibling override edges** (graph phase 1G): subclasses of the
  same resolved base that define the same method name are now connected
  pairwise — including when the base never defines the method (the
  duck-typed per-backend dispatch shape the failure taxonomy measured as
  a blind spot). Families larger than 6 are skipped (hub protection);
  dunder methods are excluded as noise.
- **Adaptive hybrid blend** (roadmap item 1, on by default in
  `analyze_impact`): the graph signal's weight is scaled by graph
  confidence (number of blast-radius candidates); freed weight moves to
  BM25. On well-connected changes the weights are exactly the frozen
  benchmarked blend — behavior only changes where the graph had little
  to say. Opt out with `analyze_impact(..., adaptive=False)`.
- **Git co-change history as a fourth signal** (roadmap item 3):
  `diffcontext.history.CoChangeIndex` mines file-level co-change
  association from `git log` (zero dependencies, graceful degradation to
  an empty index outside git repos). Opt in via
  `analyze_impact(..., history=CoChangeIndex(repo))` or
  `diffcontext compile --with-history`. This is the only signal that can
  reach co-change partners with no structural or lexical connection —
  the measured cross-subsystem ceiling. Exported as
  `diffcontext.CoChangeIndex`.
- **Benchmark: leakage-controlled evaluation of the history signal.**
  eval_v2 gained `hybrid_adaptive`, `hybrid_cochange`, and `hybrid_full`
  methods; the co-change index used for scoring is mined with every
  evaluated commit excluded, so the signal never contains the commit it
  is tested on. The Django failure buckets now also report
  `hybrid_full`, directly measuring the cross-subsystem fix.
- **Benchmark: paired significance testing** (`benchmarks/significance.py`):
  two-sided Wilcoxon signed-rank over per-commit metric pairs against
  every baseline, with Holm-Bonferroni adjustment. Pure stdlib.
- **docs/RESEARCH.md**: positioning against the 2024–2026 repo-context
  literature (GraphCoder, RepoGraph, LocAgent, CodexGraph, RepoHyper,
  CoCoMIC, …), the claim candidates the current evidence supports, and
  the explicit gap list for an ICSE/FSE/ASE-grade submission.

### Fixed
- `benchmark_runner.py --clone` cloned with `--depth=100` while printing
  "full git history" — silently starving both ground-truth mining
  (24 vs 74 usable commits on flask) and the co-change signal. Clones
  are now full-history.

## [0.3.0] — 2026-07-20

### Changed (supported Python versions)
- `requires-python` moved from `>=3.8` to `>=3.9`; Python 3.8 reached
  end-of-life in October 2024. The CI matrix now tests 3.9–3.13.

### Added (lint + typing gates in CI)
- Ruff (pyflakes + core pycodestyle) and mypy now run in CI. Typing uses
  a frozen 8-module baseline in pyproject.toml that may only shrink;
  every other module — and every new module — is checked from day one.

### Changed (hybrid blend weights — LORO-validated)
- `HYBRID_WEIGHTS` is now (0.3, 0.5, 0.2) (graph, BM25, same-file), the
  leave-one-repo-out-selected blend from the rigor pass; the original
  (0.5, 0.35, 0.15) was mildly graph-overfit from same-repo tuning.
  Measured effect at the new weights (loro_3leg.json, independently
  confirmed by a fresh check_regression run on flask: hit 0.863 / recall
  0.694, matching the recorded analysis): +1.2 to +2.4 recall points on
  4/5 dev repos, within ±1.1 (n.s.) on the four never-touched validation
  repos. `eval_v2_hardened.py`'s hybrid method now imports the product's
  weights so the benchmark always measures what ships.

### Added (`--cutoff gap` — the measured precision lever)
- `compile`/`verify` (CLI) and `compile()`/`select_context()` (library)
  accept a cutoff policy. `gap` cuts the ranking at the largest relative
  score drop in the top 50 instead of keeping a fixed top-k — measured
  F1-optimal on all five benchmark repos: ~4× the precision of top-20 at
  6–9 retrieved symbols, ~30% relative recall cost
  (RIGOR_REPORT_2026-07.md §7). Opt-in; top-k stays the recall-first
  default. Cut symbols are disclosed in the DROPPED manifest as always.
- `verify` now reports a per-case and mean **precision lower bound**
  (`precision_lb`) next to recall, so the top-k vs gap tradeoff is
  measurable on your own repo's history (measured on click, 25 history
  cases: 18.0 → 6.0 symbols/case, precision ≥33% → ≥45%, recall 72% →
  35%): `verify --from-history 20 --cutoff gap` vs the same without.

### Changed (evidence-aware sufficiency score — measured fix)
- The sufficiency score no longer treats absence of evidence as a perfect
  1.0: it shrinks toward 50 ("don't know") in proportion to missing
  observations, reports an `evidence` fraction, and keeps the legacy value
  as `score_legacy`. Measured at scale (calibration_at_scale.py, clean
  indexes): legacy r=0.016 with recall on 1,080 Python cases (the old
  citable r=0.274 was a polluted-index artifact) and constant-~100 on TS;
  evidence-aware r≈0.29 (p=0.0001) on both Python (n=1080) and TS (n=379).

### Added (learned per-repo calibration — `--save-calibration`)
- `verify --calibrate` fits a dependency-free least-squares recall
  predictor over runtime features (score components + selected /
  missing-direct / dropped-high counts + tokens); `--save-calibration`
  persists it to `.diffcontext-calibration.json`, and later `verify` runs
  report a calibrated recall estimate. Validated leave-one-repo-out:
  beats predict-the-mean on held-out MAE in 8/9 Python repos (r to 0.65);
  re-fitting the four component weights alone is a measured null.

### Added (benchmark rigor pass — see benchmarks/RIGOR_REPORT_2026-07.md)
- True dense baseline run (all-MiniLM-L6-v2): corrects §8's TF-IDF
  stand-in — BM25 is again the strongest single baseline; the pydantic
  "hybrid loses to embedding" claim is retracted; dense uniquely cracks
  the cross-subsystem ceiling (25% vs 0%) and as a fourth blend leg gives
  the first significant recall gains (flask/httpx/pydantic, p<0.05, LORO).
- Leave-one-repo-out weight validation: shipped 0.5/0.35/0.15 was mildly
  graph-overfit; honest recommendation [0.3, 0.5, 0.2]; held-out damage
  ≤2.4 recall points, n.s. Adaptive per-query blending: null result.
- Ground-truth validity measured (gt_validity.py): FP-future-co-change
  lift 1.1–8× over random; adjusted precision stays <0.15 — GT noise does
  not explain the precision problem. Largest-gap cutoff measured as the
  F1-optimal operating point on 5/5 repos (~4× top-20 precision).
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
