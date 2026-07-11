# DiffContext

**Hybrid code-retrieval engine for LLM context compilation.** Given a code
change, DiffContext finds the other functions most likely to matter and
compiles them into the smallest useful context package for a model — the
selection primitive an AI coding-agent harness calls on every loop iteration.

- Benchmarked against real developer behavior: **423 distinct commits across
  django, flask, click, httpx, and pydantic**, with baselines, confidence
  intervals, and a published failure taxonomy ([benchmarks/EVAL_V2_REPORT.md](benchmarks/EVAL_V2_REPORT.md))
- **86% hit rate / 69% recall** (cross-repo per-commit mean) at a 95–99%
  token reduction versus pasting the codebase
- Retrieval quality is **CI-gated**: every push re-runs the benchmark and
  fails if quality drops below frozen floors
- Zero runtime dependencies; Python 3.8+

```
git change ──► changed functions ──► hybrid retrieval ──► token budget ──► LLM-ready context
                                     graph ∪ BM25 ∪ file      top-k + tokens
```

## Why hybrid retrieval

Most context tools pick one signal. We benchmarked them against each other on
real co-change history — *"a developer changed these functions together;
shown one, can you find the others?"* — and the honest result is that **no
single signal wins**:

| Signal | Hit | Recall | Where it's blind |
|---|---|---|---|
| Call graph alone | 0.748 | 0.558 | Thematically-related code that never calls each other |
| BM25 keywords alone | 0.822 | 0.619 | Structure; ranks lexical noise |
| Same file alone | 0.693 | 0.506 | Anything cross-file |
| **Hybrid (this product)** | **0.856** | **0.690** | See [limitations](#known-limitations-measured-not-guessed) |

The default scoring is a normalized blend — **graph 0.5 / BM25 0.35 /
same-file 0.15** — the configuration that won recall on 4 of 5 benchmark
repos (statistically significant on Django, the largest). `--graph-only`
restores the pure call-graph signal when you want structural certainty only.

## Quick start

```bash
git clone https://github.com/trakshan-mishra/Diffcontext.git
cd Diffcontext
pip install -e .

# Index any Python repo (cold: seconds; warm re-index: ~0.02s)
diffcontext index /path/to/project

# Who is affected if I change this function? (works on uncommitted code)
diffcontext blast --changed ./src/auth.py:validate_jwt

# Compile LLM-ready context for a change
diffcontext compile --changed ./src/auth.py:validate_jwt --max-tokens 8000

# From a git diff instead of a named symbol
diffcontext compile --ref HEAD~1

# Machine-readable
diffcontext compile --changed ./src/auth.py:validate_jwt --json
```

Useful `compile` flags: `--top-k 20` (default) caps context at the
benchmarked sweet spot — ~89% of achievable recall at ~2.8× the precision of
unlimited retrieval; `--graph-only` disables the hybrid blend.

Symbol IDs are always `./relative/path.py:ClassName.method` — no
parentheses, no arguments.

## Architecture

```
                 ┌─────────────────────────────────────────────────┐
                 │              RepositoryIndex (cached)            │
   *.py files ──►│  scanner ─► parser ─► resolver ─► graph_builder  │
                 │     content-addressed SQLite cache (cache.py):   │
                 │     unchanged repo ~0.02s · 1-file edit ~0.5s    │
                 └───────────────────────┬─────────────────────────┘
                                         │
  git diff ─► diff/git_diff ─► changed symbols
                                         │
                 ┌───────────────────────▼─────────────────────────┐
                 │                 analyze_impact                   │
                 │  impact/blast_radius: callers/callees traversal  │
                 │  impact/scoring:  graph decay scores      (0.50) │
                 │  lexical.py:      BM25 over symbol source (0.35) │
                 │  same-file co-location                    (0.15) │
                 └───────────────────────┬─────────────────────────┘
                                         │  ranked candidates
                 ┌───────────────────────▼─────────────────────────┐
                 │              select + compile                    │
                 │  context/selector: token budget + top-k, no      │
                 │    silent overruns                               │
                 │  context/compiler: text render + structured      │
                 │    items + honest meta (what was DROPPED)        │
                 └──────────────────────────────────────────────────┘
```

```
diffcontext/
├── pipeline.py          # Orchestrator: index → impact → compile; hybrid blend
├── models.py            # Symbol, RepositoryIndex, ImpactResult, ContextPackage
├── scanner.py           # File discovery
├── parser.py            # AST symbol extraction
├── resolver.py          # Import → filesystem path resolution
├── symbols.py           # Attribute / local-var type tracking
├── graph_builder.py     # Dependency graph (calls, inheritance, decorators…)
├── lexical.py           # BM25 signal — pure stdlib, inverted index
├── cache.py             # Content-addressed SQLite persistence
├── diff/                # git diff / snapshot → changed symbols
├── impact/              # blast radius, scoring, traversal, terminal trees
├── context/             # token-budget selection, context compilation
└── cli/                 # index · impact · diff · compile · blast
```

## Use as a library

```python
from diffcontext.pipeline import index_repository, analyze_impact, compile

idx = index_repository("/path/to/repo")
impact = analyze_impact(idx, ["./src/auth.py:validate_jwt"])   # hybrid by default
ctx = compile(idx, impact, max_tokens=8000, top_k=20)

print(ctx.text)                      # paste-ready context with meta-header
print(ctx.dropped_symbols[:5])       # what the budget cut — never hidden
```

### In an agent harness (incremental API)

Built to be called on every agent-loop iteration — repeat calls are cheap and
output is structured, not just a string:

```python
from diffcontext.pipeline import index_repository, analyze_impact, compile
from diffcontext import ScoringConfig

idx = index_repository("/path/to/repo")     # cold: full parse + graph build

# ... agent edits src/auth.py ...
idx.update(["src/auth.py"])                 # re-parses ONLY the changed file

impact = analyze_impact(idx, ["./src/auth.py:validate_jwt"],
                        scoring_config=ScoringConfig())    # weights tunable
ctx = compile(idx, impact, max_tokens=8000,
              token_counter=my_real_tokenizer)             # e.g. tiktoken

for item in ctx.items:       # structured: re-budget/filter/reorder yourself
    print(item.symbol_id, item.role, item.score, item.token_estimate)
```

Measured on pydantic (405 files, ~1,830 symbols): cold index ~2.6–4.2s;
re-index of an unchanged repo ~0.02s (graph persisted content-addressed in
`.diffcontext_cache.db`, so a new process gets the warm path);
`index.update()` after a one-file edit ~0.4–0.6s vs ~1.6s full re-index —
verified equal to a from-scratch rebuild by the test suite.

## Benchmarks

The full methodology and results — distinct-commit sampling, four baselines,
bootstrap CIs, a budget sweep, and a hand-audited failure taxonomy — are in
[benchmarks/EVAL_V2_REPORT.md](benchmarks/EVAL_V2_REPORT.md). Headline
(per-commit hit rate / recall, hybrid):

| | django | click | flask | httpx | pydantic |
|---|---|---|---|---|---|
| Hit | 0.887 | 0.877 | 0.831 | 0.934 | 0.753 |
| Recall | 0.782 | 0.727 | 0.667 | 0.756 | 0.517 |

Reproduce everything:

```bash
pip install rank-bm25                          # benchmark-only dependency
python benchmark_runner.py --clone             # clone the five eval repos
python benchmarks/eval_v2_hardened.py          # full run (~10 min)
python benchmarks/check_regression.py          # the CI quality gate (~1 min)
```

The gate (`check_regression.py`) enforces frozen hit/recall floors and runs
in CI on every push — retrieval quality cannot silently regress.

## What the resolver handles

Asserted by the test suite on real resolved edges, not "it ran":
multi-hop attribute chains (`self.a.b.method()`), multiple inheritance and
cross-file MRO, circular imports, local-variable instantiation in free
functions, annotated-parameter receivers, import aliasing, sibling-directory
bare imports, decorator wrapper attribution, `src/`-layout packages
(`import black` resolving to `src/black/`), module-attribute calls through
package re-exports (`black.parse_ast()` → `black/parsing.py`), dotted module
calls (`import a.b; a.b.fn()`), and function references passed as arguments
(`functools.partial(fn, ...)`, `sorted(xs, key=fn)`) with parameter-shadowing
guarded against.

## Known limitations (measured, not guessed)

From the failure taxonomy — 60 hand-audited Django co-change pairs with no
call-graph connection:

- **Thematic siblings** (same feature, no call between them): the graph is
  blind; the BM25 leg recovers these partially. *Fixable* — an adaptive
  blend is the top roadmap item.
- **Dispatch/override pairs** (same method name across a hierarchy):
  *partially fixable* via synthetic override edges in the graph.
- **Cross-subsystem conceptual links** (e.g. a settings flag and the
  security check that reads it): graph, BM25, and hybrid all score **0/20**.
  A structural ceiling for every static-analysis retriever — reachable only
  with signals like git co-change history (roadmap item 3).
- **Dynamic dispatch** (`getattr(obj, name)()` with runtime `name`) and
  metaclass-generated code are statically unresolvable — this is why
  pydantic is the weakest benchmark repo for every method tested.

When in doubt: `grep -rn "function_name(" --include="*.py" .` before fully
trusting "no callers found."

## Web service

A FastAPI service + single-file web UI lives in
[diffcontext-service/](diffcontext-service/): clone a GitHub repo by URL,
index it, and query blast radius / search / context over HTTP. The
[Dockerfile](Dockerfile) packages it for container platforms
(e.g. Hugging Face Spaces).

```bash
pip install fastapi uvicorn python-multipart aiofiles
uvicorn diffcontext-service.backend.main:app --port 8000
```

## Testing

```bash
python3 -m pytest tests/ -q      # 68 tests, self-contained, <1s
```

## Roadmap

Ordered by measured impact (see the failure taxonomy above):

1. **Adaptive blend** — up-weight BM25 when graph confidence is low
2. **Override edges** — link same-named methods across class hierarchies
3. **Git co-change history as a fourth signal** — the only known path past
   the cross-subsystem ceiling
4. **Calibrated confidence scores** — adaptive context cutting for agents
5. **TypeScript support** — the architecture is language-agnostic; only
   `parser.py`/`graph_builder.py` are Python-specific

Longer-form planning notes: [docs/PLAN.md](docs/PLAN.md).

## License

MIT
