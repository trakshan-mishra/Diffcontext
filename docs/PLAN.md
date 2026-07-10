# DiffContext — Path to: Published PyPI Library, Core Agent-Harness Infrastructure, Research-Grade Benchmark

*Full read of `main` (commit `6be96d0`, which already contains everything merged from `stable` and `feature/eval-v1-benchmark` — the latest of the repo's 3 branches). MCP server is explicitly deferred/out of scope per current direction.*

---

## 0. The ask, restated precisely

Three separate bars, in increasing order of difficulty:

1. **A real PyPI package.** Something you `pip install diffcontext` and it Just Works, with a stable public API other projects can depend on.
2. **The context/loop-engineering primitive for AI coding agents.** Not a CLI toy — the thing an agent *harness* (Claude Code-style, Aider-style, SWE-agent-style) calls on every loop iteration to decide what code to put in the model's context window. This means the bar is "embeddable infrastructure," not "nice CLI output."
3. **A benchmark rigorous enough to defend in a paper.** The eval machinery already in `benchmarks/eval_v1.py` is unusually good for a solo project (P@K/R@K/MRR/MAP/nDCG@20, bootstrap CIs, per-signal ablation, failure taxonomy) — but there are specific gaps that a reviewer would flag in the first pass, detailed below.

These three pulls are in tension in one way worth naming up front: (2) wants the library *fast and stateful* (index once, mutate incrementally, called hundreds of times per agent session); (1) wants it *simple and stable* (a clean pip package with a frozen API); (3) wants the *heuristics themselves* to be defensible (not overfit to the handful of repos already used for tuning). The plan below sequences things so each pillar's work de-risks the next, but Fable 5 should know these three "customers" of the same codebase can pull in different directions and treat that explicitly rather than silently picking one.

---

## 1. Pillar A — PyPI-ready library

### Concrete blockers found (fix these regardless of everything else)

| Issue | Where | Why it matters |
|---|---|---|
| **Version mismatch** | `pyproject.toml:7` says `0.1.0`; `diffcontext/__init__.py:35` says `__version__ = "0.2.0"` | `pip show diffcontext` and `diffcontext.__version__` will disagree the moment this ships. Any harness that gates behavior on version (exactly the kind of consumer Pillar B targets) breaks immediately. |
| **No `LICENSE` file** | repo root | README claims MIT; there is no `LICENSE` file at all. PyPI's own upload UI, GitHub's license detector, and most corporate legal-approval bots for adding a new dependency all look for this file specifically, not just a README claim. |
| **Placeholder author** | `pyproject.toml:12-14`: `authors = [{ name = "Your Name" }]` | Ships to PyPI as literally "Your Name" unless fixed. Trivial but visible. |
| **Declared but missing `py.typed`** | `pyproject.toml:37-38` declares `package-data = ["py.typed"]`, but no `py.typed` file exists anywhere in `diffcontext/` | Type-checker consumers (mypy/pyright — exactly what a serious harness integration would run) get no inline type-checking signal from the published wheel even though the package *claims* to be typed. Either add the marker file for real, or stop claiming it. |
| **`mcp` extra + broken entry point** | `pyproject.toml:26-32` | Per your direction, MCP is deferred. **Strip the `mcp` extra and the `diffcontext-mcp` script entirely from `pyproject.toml` for this release** rather than shipping a declared entry point (`diffcontext.mcp_server:main`) that points at a file that doesn't exist. Revisit as its own follow-up once Pillar B's incremental-index work lands (an MCP server is exactly the "many small repeat queries" workload that needs that work first). |
| **No release automation** | n/a | No GitHub Actions workflow exists for anything, including a release-to-PyPI pipeline. |

### What "stable public API" needs to mean

`diffcontext/__init__.py` already has the right *shape* of a public API (`index()`, `diff()`, `blast_radius()`, `compile_context()`, typed dataclasses `RepositoryIndex`/`ImpactResult`/`ContextPackage`/`BlastResult`). Before this is versioned and published, decide and document explicitly:

- What's public API (semver-covered) vs. internal (`graph_builder`, `resolver`, `symbols` internals — currently importable by anyone, with no `__all__` anywhere marking the boundary).
- A semver policy: since Pillar B wants harnesses depending on this directly, breaking the return shape of `ContextPackage` or `ImpactResult` in a patch release would break every embedder. Add `__all__` to `diffcontext/__init__.py`, and treat everything under it as the only thing covered by semver guarantees.
- `CHANGELOG.md` starting now — even a one-line-per-release file — because Pillar B consumers will need to know what changed between versions they're pinning to.

### Release mechanics
- GitHub Actions workflow: on tag push, build sdist+wheel, run the full test suite, publish via PyPI **trusted publishing** (OIDC, no long-lived API token to leak) — this is the current PyPI-recommended flow and worth doing correctly from release #1 rather than retrofitting.
- Verify `python -m build` actually produces a clean wheel (no stray fixture/benchmark files leaking into the package — currently fine, since `[tool.setuptools.packages.find] include = ["diffcontext*"]` correctly scopes it, but this should be a CI-checked assertion, not an assumption).

---

## 2. Pillar B — the harness/loop-engineering infrastructure bar

This is the part that changes the *design*, not just the packaging. If the target user is an agent loop calling this dozens of times per session, several things that were "nice to have" for a CLI tool become load-bearing.

### 2.1 Speed on repeat calls is now a correctness requirement, not a UX nice-to-have

Already flagged in the earlier review, but it matters more now: `graph_builder.build_repository_graph()` (`graph_builder.py:64-134`) re-reads and re-parses every file itself, independent of the cached `extract_all_symbols()` path — and the graph build is the expensive stage (README's own numbers: up to ~7.9s on a Pydantic-sized repo). An agent harness calling this every loop iteration pays that cost *every single call*. This isn't a latency inconvenience anymore — for a tight edit-check-edit-check agent loop, an 8-second stall per check is disqualifying, not just annoying.

**Concrete design target:** an incremental index.
- `index_repository(path)` → full build (as now), but the result should be a *mutable* object that supports `index.update(changed_files)` — re-parse and re-graph only the changed files and the edges touching them, not the whole repo. This is the single highest-leverage architectural change for Pillar B specifically.
- Persist the graph (not just symbols) keyed by content hash, same pattern `cache.py` already uses for symbols — extend it to graph edges so a second process invocation (or a second turn in a stateless harness that re-instantiates the library) doesn't pay full cost either.

### 2.2 In-process statefulness across a loop

The API already separates `index_repository()` from `analyze_impact()`/`compile()`, which is the right shape (index once, query many times within one process) — that's a genuine strength worth keeping. The gap is there's no *update* path for that same in-memory index when the agent's next action changes a file. Right now the only option is a full re-`index_repository()` call, discarding everything. Add `RepositoryIndex.update(changed_files: List[str])` as the core Pillar-B-facing API.

### 2.3 Structured, not just textual, output for machine consumers

`compile()`'s output (`ContextPackage.text`) is a formatted string meant for a human/LLM to read directly — great for the CLI's use case, wrong shape for a harness that wants to make its *own* decisions about what to inject into its context window (e.g., a harness that has its own token budget across multiple tools, not just this one). Add a structured variant: a list of `{symbol_id, code, score, role: CHANGED|IMPACTED|DEPENDENCY, callers, callees, token_estimate}` objects that a harness can filter/reorder/re-budget itself, with the current formatted text as one renderer built on top of that structure (not the other way around, as it is now).

### 2.4 Token accounting accuracy

`context/selector.py:100-101`'s `_estimate_tokens` is a flat 4-chars-per-token heuristic with a 20% buffer. Fine for a human pasting into a chat window; not fine for a harness enforcing a hard context-window limit against a real tokenizer (tiktoken for GPT models, Anthropic's counting for Claude). Make the tokenizer pluggable (`Callable[[str], int]` parameter, defaulting to the current heuristic) so harness integrations can pass their model's exact tokenizer instead of eating the approximation error.

### 2.5 Concurrency/thread-safety, since harness processes are long-lived

Two things that were fine for a short-lived CLI process become real issues in a long-running harness process handling concurrent tool calls: the module-level `_warned_files`/`_warned_encoding_files` globals in `_warn_once.py` (fine for dedup within one CLI run; wrong for a persistent process serving many repos/sessions — warnings for repo B's file could get silently suppressed because repo A happened to hit a same-named file first), and `SymbolCache`'s single sqlite connection (no protection against concurrent access from multiple threads in one process). Scope the warn-dedup state and the cache connection to the `RepositoryIndex`/session, not module-global.

### 2.6 Pluggable scoring

`impact/scoring.py`'s constants (`CALLEE_DECAY`, `CALLER_DECAY`, `STRUCT_MAX`, etc.) are module-level and fixed. A harness doing, say, a broad refactor-impact-check loop vs. a narrow bug-fix loop plausibly wants different caller/callee weighting. Expose these as a `ScoringConfig` dataclass parameter with the current values as defaults, rather than hardcoded module constants — this also directly serves Pillar C (ablations become "try these configs" instead of "edit the source and re-run").

---

## 3. Pillar C — making the benchmark defensible in a paper

The eval machinery that exists (`benchmarks/eval_v1.py`, `benchmarks/ground_truth.py`, `benchmarks/baselines.py`, `benchmarks/diagnose_graph_gaps.py`) is genuinely more rigorous than most solo-project benchmarking — P@K/R@K/MRR/MAP/nDCG@20, 1000-resample bootstrap CIs, per-signal ablation (Graph | BM25 | File | Random | combinations), and a real failure taxonomy already exist as working code, not just aspiration. That's the good news — this is not starting from zero. The gaps below are specifically the ones a paper reviewer (or a skeptical HN commenter) would raise in the first five minutes.

### 3.1 Train/test leakage — the single biggest risk to credibility

The README states the resolver's heuristics were "iteratively debugged against real production codebases (openai/whisper, pallets/click, pallets/flask)," and the benchmark table reports numbers on Flask, Click, HTTPX, and Pydantic — **the same or overlapping set the heuristics were tuned against.** Window size (3), decay constants (0.65/0.85), edge-type caps (`_SHARED_IMPORT_MAX_CONSUMERS=10`, `_SAME_DIR_MAX_FILES=20`, `PARENT_CHILD_MAX_CHILDREN=8`) all look like they were hand-tuned by running against real repos and checking results — which is a legitimate way to *build* the heuristics, but reporting benchmark numbers on the same repos used to tune them is the textbook train/test leakage a reviewer flags immediately.

**Fix: a strict dev/held-out split.** Freeze the current tuning set as "dev" (used to set constants), pick a *disjoint* set of repos never looked at during heuristic development as "held-out," and report both numbers separately in any paper claim — with the held-out numbers as the actual headline result. This is non-negotiable for publishability; everything else below is secondary to this.

### 3.2 Sample size and diversity

Four to five repos isn't enough for a statistical claim, and they're all somewhat similar in kind (Python web/CLI/validation libraries). For a defensible paper: a stratified sample of ~20-50 repos across categories (web framework, CLI tool, data/validation library, ML/data-science library, async/networking library, a large monorepo-style app) sampled by a documented, non-cherry-picked method (e.g., "top-N Python repos by stars in category X on date Y, commit SHA pinned").

### 3.3 Ground-truth methodology needs to be formalized and defended, not just implemented

Co-change-from-git-history is a legitimate, literature-precedented proxy for "these functions are related" — but it has known confounds (mass-rename commits, formatting-only commits, and large multi-purpose commits inflate co-change counts without semantic relatedness). `run_benchmark.py`'s docstring already mentions filtering out same-file-only co-changes — good instinct — but this needs to become a written, defended methodology section (what commits are excluded and why), not an implementation detail buried in a script docstring, and ideally spot-checked against a small hand-labeled sample to report inter-rater-style confidence in the proxy itself.

### 3.4 Baseline coverage gap: no embedding/dense-retrieval baseline

Current baselines (`benchmarks/baselines.py`): BM25, file-colocation, random. That's a good start but misses the baseline every reviewer will ask about first: a standard dense-retrieval/RAG baseline (embed each function with an off-the-shelf code embedding model, retrieve by cosine similarity). Without this, the paper can't answer "how does this compare to what everyone is actually shipping today (embedding-based RAG)" — which is the paper's whole reason for existing. This is the most important addition to the benchmark suite for research credibility specifically.

### 3.5 Statistical testing, not just confidence intervals

Bootstrap CIs (already implemented) tell you the uncertainty of one method's score; they don't by themselves establish that method A beats method B. Add paired significance testing across the same eval cases (e.g., Wilcoxon signed-rank test between DiffContext and each baseline, per metric) so "graph beats BM25" is a stated p-value, not an eyeballed CI-overlap.

### 3.6 Reproducibility package
- Pin every benchmarked repo to an exact commit SHA (not "flask" but "pallets/flask@<sha>").
- A single `requirements-benchmark.txt`, fully pinned (note: `benchmarks/baselines.py` imports `rank_bm25`, which isn't declared anywhere in `pyproject.toml` or a requirements file today — undeclared dependency for anyone trying to reproduce the benchmark).
- A single command that regenerates every number in the paper from a clean checkout.
- The "freeze benchmark eval_v1 config and save baseline results" commit already in the git history is the right instinct — make that snapshot the actual artifact a paper cites, with its own tag.

### 3.7 What's already publishable as-is (don't rebuild this)
- The ablation design (per-signal: Graph alone, BM25 alone, File alone, Random, and combinations) is exactly the shape of "Table 2" in a paper like this.
- `diagnose_graph_gaps.py`'s failure taxonomy (isolated query vs. isolated GT vs. same-file-different-class vs. different-package, etc.) is most of a "Section 5: Error Analysis" already.
- The "known limitations" section in the README (dynamic dispatch, theme-not-call-graph relatedness, user-defined higher-order functions) is honest and precise — exactly the tone a paper's limitations section needs, just needs to move from README prose to a proper written section with the failure-taxonomy data backing each claim quantitatively.

---

## 4. Sequenced roadmap for Fable 5

Ordered so each phase de-risks the next; Pillar A and the P0/P1 fixes from the prior review are prerequisites, not alternatives, to B and C.

**Phase 1 — Make the package not embarrassing (Pillar A, mechanical)**
Fix version mismatch, add real `LICENSE`, fix author field, either add a real `py.typed` or stop declaring it, strip the `mcp` extra/entry point per current direction, add `__all__` to `diffcontext/__init__.py`, start `CHANGELOG.md`, add a GitHub Actions test workflow (none exists today) and a tag-triggered PyPI-publish workflow via trusted publishing.

**Phase 2 — Fix the stale-output bug and close the biggest test gaps** (carried over from the prior review — still true and still important)
Fix `context/compiler.py`'s hardcoded, now-inaccurate "scoring basis" string (derive it from `scoring.py`'s live constants). Add tests for `cache.py`, `diff/git_diff.py`, and the compiler meta-header specifically (a test asserting the meta text matches live constants would have caught the bug above and prevents it recurring).

**Phase 3 — Incremental index (Pillar B's core deliverable)**
Stop double-parsing (`graph_builder` should consume the already-parsed ASTs/symbols instead of re-reading files itself). Add persistent, content-hash-keyed graph caching (extending the existing `SymbolCache` pattern). Add `RepositoryIndex.update(changed_files)` for in-process incremental updates. This is the change that makes "called every loop iteration by an agent harness" actually viable instead of aspirational.

**Phase 4 — Harness-facing API surface (Pillar B, rest of it)**
Structured (non-text) selection output as the base representation, pluggable tokenizer, pluggable `ScoringConfig`, scope `_warn_once`/cache state off module-globals and onto the session/index object.

**Phase 5 — Benchmark hardening (Pillar C)**
Held-out repo split (highest priority sub-item — do this before anything else in this phase), expand repo sample size/diversity, add an embedding-based dense-retrieval baseline, add paired significance testing, pin everything for reproducibility, and only then move to actually drafting the paper (outline below) — the paper should be written from results that have already survived the held-out-split and baseline-strengthening, not before.

**Phase 6 — Consolidation (from the prior review, still applies)**
Pick `benchmarks/eval_v1.py` as canonical; remove or archive `benchmark_runner.py`/`run_benchmark.py`/`run_metrics.py` at the repo root. Extract the duplicated CtxSync push logic into one client module with a real timeout.

---

## 5. Research paper skeleton (for Phase 5's endpoint)

A realistic target given no academic affiliation: an arXiv preprint / technical report, written to the bar of an LLM-for-code workshop paper (e.g., the kind co-located with ICSE/FSE or an ICLR/NeurIPS workshop track), not necessarily a full conference submission.

1. **Title/Abstract** — frame as: structural (call-graph) context selection vs. embedding-based retrieval for LLM code-editing tasks, evaluated via a co-change proxy from real git history.
2. **Related work** — position against: RAG-for-code / embedding retrieval, Aider's repo-map, Sourcegraph Cody's context engine, static-analysis graph approaches (CodeQL-style, code2vec/GraphCodeBERT-era graph learning), and the emerging "context/harness engineering" framing from agentic-coding tool blog posts (a real, citable trend right now, not just an academic angle).
3. **Method** — precise, reproducible description of graph construction (edge types listed in `graph_builder.py`'s own module docstring are almost paper-ready as written) and the decay-based scoring algorithm.
4. **Experimental setup** — the held-out split (§3.1), repo sample and selection methodology (§3.2), ground-truth/co-change methodology and its filtering (§3.3).
5. **Results** — headline metrics on held-out set only; per-signal ablation table; graph vs. BM25 vs. file-colocation vs. random vs. dense-embedding baseline, with significance tests.
6. **Error analysis** — built directly from `diagnose_graph_gaps.py`'s existing failure taxonomy, quantified over the held-out set.
7. **Limitations** — dynamic dispatch, non-call-graph relatedness, user-defined higher-order functions (already well-articulated in the README; needs to move into the paper with supporting numbers from the failure taxonomy).
8. **Reproducibility statement** — pinned commits, frozen eval config, public benchmark code (already MIT-licensed once §1's LICENSE fix lands).

---

## 6. Open questions before Fable 5 starts

- **Held-out repos**: do you want to hand-pick the held-out set now (so heuristics are never touched again after this point), or should Fable 5 propose a sampling methodology and pick them programmatically? (Recommend the latter — removes any appearance of cherry-picking, which matters more for the paper than for the library.)
- **Publish cadence**: ship Phase 1 (PyPI-ready package) as soon as it's done, independent of Phases 3-5 landing? Or hold the first PyPI release until the incremental-index work (Phase 3) is in, so v0.1/v1.0 on PyPI is already the "harness-ready" version rather than shipping a slow v0.1 first and a breaking v0.2 later? (Recommend: ship Phase 1+2 as an honest `0.2.x` now — fixing real bugs and packaging hygiene doesn't need to wait — then treat Phase 3's incremental index as the `1.0.0` milestone, since that's the point the public API becomes something worth promising semver stability on.)
- **Paper venue ambition**: arXiv-only technical report (fast, no review gate, citable immediately) vs. actually targeting a workshop submission (slower, adds a real review bar, but carries more weight)? This affects how much of Phase 5 is "good enough to publish code + numbers" vs. "good enough to survive peer review."
