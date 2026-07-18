# DiffContext

**Find the code that matters for a change, and fit it into an LLM's context
window — automatically.**

```
git change ──► changed functions ──► hybrid retrieval ──► token budget ──► LLM-ready context
                                     graph ∪ BM25 ∪ file      top-k + tokens
```

- Zero runtime dependencies, Python 3.8+, `pip install -e .`
- Indexes **Python** (full resolver, benchmarked below) and — with the
  optional `[typescript]` extra — **TypeScript/JavaScript** (experimental;
  measured separately, see [Language support](#language-support))
- Benchmarked on **423 real commits across django, flask, click, httpx,
  pydantic** — plus independent validation runs on black and requests
- **~2× the recall of grep at every token budget** on real co-change ground
  truth — at **~5-10% precision**: most of what's retrieved is supporting
  context around the change, not the exact co-change set, so you get a wide
  net with the right things in it, not a curated shortlist
  ([details below](#does-it-actually-work-measured-not-claimed))
- Retrieval quality is **CI-gated**: every push re-runs the benchmark and
  fails the build if quality drops

---

## The problem (why this exists)

Say you ask an AI assistant to help you change one function in a
50,000-line project. You have three options, and they're all bad:

1. **Paste the whole repo.** It doesn't fit in the context window. Even
   when it does, you pay for 200k tokens of code so the model can use 2k
   of it — and models get *worse* at finding things in huge contexts.
2. **Paste just the function.** Now the model can't see who calls it,
   what it calls, or the sibling function that must change in lockstep.
   It will confidently propose a fix that breaks three callers it never saw.
3. **Grep for the function's name and paste the matches.** Better — but
   grep only finds code that *mentions the name*. The config check that
   must change together with your function, the subclass that overrides
   it, the handler that receives it through `functools.partial` — none of
   those necessarily contain the string you grepped for. (We measured
   this: grep's recall *plateaus* no matter how much budget you give it.
   See the numbers below.)

DiffContext is option 4: **understand the repository's structure once,
then, for any change, select the few functions that actually matter and
compile them into the smallest useful package for the model.**

## The intuition (how a change "spreads" through code)

When a developer changes a function, the *other* code they end up touching
in the same commit tends to be related in one of three measurable ways:

1. **Connected in the call graph.** You changed `validate_jwt`; whoever
   *calls* `validate_jwt` might break, and whatever `validate_jwt` *calls*
   explains how it works. This is the strongest signal — but it's blind to
   related code that never calls yours.
2. **Lexically similar.** Two functions full of the same rare words
   (`refresh_token`, `jwks_cache`) are usually about the same thing, even
   with no call between them. Classic search-engine ranking (BM25) finds
   these — but it also ranks up noise that merely *sounds* similar.
3. **In the same file.** Code that lives together changes together. Weak
   but cheap, and it catches things the other two miss.

None of these wins alone — we benchmarked each against real commit history
and each has a measurable blind spot:

| Signal | Hit | Recall | Where it's blind |
|---|---|---|---|
| Call graph alone | 0.748 | 0.558 | Related code that never calls yours |
| BM25 keywords alone | 0.822 | 0.619 | Structure; ranks lexical noise |
| Same file alone | 0.693 | 0.506 | Anything cross-file |
| **Hybrid (this product)** | **0.856** | **0.690** | See [limitations](#known-limitations-measured-not-guessed) |

So DiffContext blends all three — **graph 0.5 / BM25 0.35 / same-file
0.15** — the exact weights that won recall on 4 of 5 benchmark repos.
`--graph-only` turns the blend off when you want structural certainty only.

## Quick start

```bash
git clone https://github.com/trakshan-mishra/Diffcontext.git
cd Diffcontext
pip install -e .

# 1. Index any Python repo (cold: seconds; warm re-index: ~0.02s)
diffcontext index /path/to/project

# 2. "Who is affected if I change this function?"
diffcontext blast --changed ./src/auth.py:validate_jwt

# 3. Compile LLM-ready context for the change, capped at 8k tokens
diffcontext compile --changed ./src/auth.py:validate_jwt --max-tokens 8000

# 4. Or start from an actual git diff instead of naming a symbol
diffcontext compile --ref HEAD~1

# 5. Machine-readable, for scripts and agents
diffcontext compile --changed ./src/auth.py:validate_jwt --json

# 6. Is the compiled context actually SUFFICIENT? Score it, test it,
#    and calibrate the score against your repo's own git history
diffcontext verify --ref HEAD~1
diffcontext verify --from-history 30 --calibrate
```

Symbol IDs are always `./relative/path.py:ClassName.method` — no
parentheses, no arguments.

Try it on a serious codebase you didn't write:

```bash
git clone https://github.com/psf/black && cd black
diffcontext index .                       # ~4s, ~650 functions
diffcontext blast --changed ./src/black/__init__.py:format_file_contents
# → correctly reports that blackd (the HTTP server, a DIFFERENT package)
#   is affected — an edge that only exists via functools.partial
```

## Don't trust our benchmarks — run yours (2 minutes)

Every retrieval number in this README was measured on repos *we* picked.
`verify` exists so you can grade DiffContext against **your** repo's real
history and your own known-true expectations — and get an honest answer,
including "this tool doesn't work well here."

```bash
cd your-repo

# 1. Mine 20 test cases from YOUR git history (symbols that actually
#    changed together in past commits) and grade retrieval against them
diffcontext verify --from-history 20 --calibrate

# 2. Save the mined cases, edit out the noise, keep what you know is true
diffcontext verify --from-history 20 --out cases.json

# 3. Re-run your curated suite any time — exit code 1 on failure, so it
#    can gate CI
diffcontext verify --cases cases.json
```

Or write a case by hand — one JSON object per expectation you know is
true about your codebase:

```json
{
  "version": 1,
  "cases": [
    {
      "name": "auth-touches-middleware",
      "changed": ["./api/auth.py:validate_jwt"],
      "must_include": ["./api/middleware.py:check_auth"],
      "min_recall": 1.0
    }
  ]
}
```

That case says: *"if I change `validate_jwt`, a sufficient context MUST
contain `check_auth`."* Symbol names are typo-checked with fuzzy
suggestions, so a wrong path fails loudly instead of silently passing.
And if calibration finds the sufficiency score doesn't track measured
recall on your repo, `verify` prints **NULL RESULT** in plain text rather
than a decorative number — finding out the tool doesn't fit your repo *is*
the feature. Full case format and methodology:
[docs/VERIFY.md](docs/VERIFY.md).

## What you get back (and why it's shaped that way)

`compile` doesn't just dump code. The output leads with a **meta header
that tells the model what it CANNOT see**:

```
=== DIFFCONTEXT META ===
Repo symbols total    : 648
Symbols IN context    : 18
Symbols DROPPED       : 630  ← you cannot see these
Graph confidence      : 100%  ✓
Context tokens (code) : 5,644
Output tokens (full)  : 7,012
...
DROPPED SYMBOLS (630) — scored but cut by token budget:
  - ./src/black/linegen.py:transform_line  (score: 71)
  ...
```

Every function in the body is annotated with its callers and callees, and
anything referenced but *not included* is tagged `[NOT IN CONTEXT]` — so
the model knows the difference between "this function doesn't exist" and
"this function exists but wasn't shown to me." That distinction is the
difference between an honest answer and a hallucinated one.

We stress-tested this honesty claim (see below): at a tight 2,000-token
budget, **0%** of the ground-truth functions DiffContext failed to include
were silently invisible — every single miss was disclosed in the dropped
manifest.

## How it works, step by step

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
                 │  context/selector: token budget + top-k          │
                 │  context/compiler: honest meta (what was         │
                 │    DROPPED) + annotated code                     │
                 └──────────────────────────────────────────────────┘
```

In plain words:

1. **Scan & parse.** Find every `.py` file, parse each one *once* into an
   AST, and extract every function/method as a `Symbol`.
2. **Resolve imports.** Turn `from auth.tokens import verify` — and
   `import black` under a `src/` layout, and re-exports through
   `__init__.py` — into actual file paths, so calls can be attributed to
   real definitions.
3. **Build the graph.** Who calls whom, who inherits from whom, who
   decorates whom — plus function references passed as *arguments*
   (`partial(fn, ...)`, `sorted(xs, key=fn)`), which are dependencies even
   though they're never "called" at that site.
4. **Cache everything.** The graph is persisted content-addressed (keyed
   by the hash of every file), so re-indexing an unchanged repo costs
   ~0.02s and editing one file re-parses only that file — verified equal
   to a from-scratch rebuild by the test suite.
5. **Score candidates.** Walk the graph outward from the changed
   function (scores decay with distance), blend with BM25 similarity and
   same-file bonus.
6. **Select & compile.** Pack the top-scoring functions into your token
   budget (default top-20 per changed symbol — the benchmarked sweet
   spot), and render with the honest meta header.

```
diffcontext/
├── pipeline.py          # Orchestrator: index → impact → compile; hybrid blend
├── models.py            # Symbol, RepositoryIndex, ImpactResult, ContextPackage
├── scanner.py           # File discovery
├── parser.py            # AST symbol extraction
├── resolver.py          # Import → filesystem path resolution (src-layouts, re-exports)
├── symbols.py           # Attribute / local-var type tracking
├── graph_builder.py     # Dependency graph (calls, inheritance, decorators, fn-refs…)
├── lexical.py           # BM25 signal — pure stdlib, inverted index
├── cache.py             # Content-addressed SQLite persistence
├── diff/                # git diff / snapshot → changed symbols
├── impact/              # blast radius, scoring, traversal, terminal trees
├── context/             # token-budget selection, honest context compilation
└── cli/                 # index · impact · diff · compile · blast
```

## Does it actually work? (measured, not claimed)

### Against real developer behavior, across repos

The core benchmark asks: *a developer changed these functions together in
one commit; shown one, can the tool find the others?* Full methodology —
distinct-commit sampling, four baselines, bootstrap confidence intervals,
budget sweep, hand-audited failure taxonomy — in
[benchmarks/EVAL_V2_REPORT.md](benchmarks/EVAL_V2_REPORT.md). Headline
(per-commit hit / recall, hybrid):

| | django | click | flask | httpx | pydantic |
|---|---|---|---|---|---|
| Hit | 0.887 | 0.877 | 0.831 | 0.934 | 0.753 |
| Recall | 0.782 | 0.727 | 0.667 | 0.756 | 0.517 |

Independent validation on repos never used for tuning: **black** hybrid
hit 0.901 / recall 0.720, **requests** hit 0.969 / recall 0.774.

The flip side of that recall, stated as plainly as the recall itself:
cross-repo mean **precision is 0.075 hybrid / 0.060 graph-only** — roughly
92-94% of retrieved symbols are not in the ground-truth co-change set.
They're mostly structurally adjacent supporting context (callers, callees,
same-file siblings), which is often what you want an LLM to see, but if
you're paying per token, precision — not recall — is this product's real
problem, and the benchmark report says so in exactly those words. The full
precision/recall tradeoff, including the per-method sweep, is in
[benchmarks/EVAL_V2_REPORT.md](benchmarks/EVAL_V2_REPORT.md).

### Head-to-head vs grep, at identical token budgets

The question that actually matters for an agent loop: *given the same
context window, does this beat what a developer does by hand?* 30 real
co-change queries from black's history; recall of the true co-change
partners inside the packed window
([benchmarks/budget_head2head.py](benchmarks/budget_head2head.py)):

| Token budget | grep-packing | DiffContext | |
|---|---|---|---|
| 1,000 | 0.083 | 0.122 | +47% |
| 2,000 | 0.145 | 0.282 | ~2× |
| 4,000 | 0.215 | 0.408 | ~2× |
| 8,000 | **0.215 (plateau)** | **0.576** | 2.7× |

Note the shape: grep **plateaus** — beyond ~4k tokens, more budget buys
nothing, because name-matching cannot find co-change partners that don't
mention the name. Graph+BM25 retrieval keeps climbing.

Honesty audit at the tight 2k budget (128 ground-truth symbols): 34% made
it into context, 66% were explicitly disclosed as dropped, **0% silently
invisible**.

### Quality can't silently regress

`benchmarks/check_regression.py` enforces frozen hit/recall floors and
runs in CI on every push. If a change to the heuristics drops retrieval
quality below the floors, the build fails.

```bash
pip install rank-bm25                          # benchmark-only dependency
python benchmark_runner.py --clone             # clone the five eval repos
python benchmarks/eval_v2_hardened.py          # full run (~10 min)
python benchmarks/budget_head2head.py benchmark_repos/black   # grep head-to-head
python benchmarks/check_regression.py          # the CI quality gate (~1 min)
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

Built to be called on every agent-loop iteration — repeat calls are cheap
and output is structured, not just a string:

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
re-index of an unchanged repo ~0.02s; `index.update()` after a one-file
edit ~0.4–0.6s vs ~1.6s full re-index — verified equal to a from-scratch
rebuild by the test suite. Stress-tested on a synthetic 1,500-file /
6,000-symbol repo: cold 3.7s, warm 0.14s, per-query impact+compile 0.15s.

**Token accounting:** `--max-tokens` budgets the symbol *code*; the meta
header and caller/callee annotations add overhead on top, which is
reported honestly (`token_estimate` and the meta's `Output tokens (full)`
line cover the entire output) and auto-compacts under tight budgets so
meta can never dwarf the code it annotates.

## Language support

| Language | Status | How | Retrieval quality |
|---|---|---|---|
| Python | **Full** | stdlib `ast`, deep resolver (see below) | Benchmarked: 423 commits, 5 repos + 2 validation repos (numbers above) |
| TypeScript / JavaScript | **Experimental** | tree-sitter adapter, `pip install -e ".[typescript]"` | Measured once, not yet benchmarked: 18/25 mined co-change cases passed, **mean recall 67.8%** on honojs/hono via `verify --from-history 25` |
| Go / Rust / Java / others | Not supported | — | Retrieves nothing |

What the TypeScript adapter resolves: functions, class methods, arrow
consts, namespaces, ES imports (named/default/namespace, aliases, barrel
`index.ts` re-exports incl. `export * from`), `this.method()`, `super()`,
`new Class()`, `extends` override edges, and function references passed
as arguments. What it deliberately does not (v1, and it lowers graph
confidence, which the meta header reports): **no type inference** —
`obj.method()` on an arbitrary object is unresolved — no tsconfig path
aliases, no CommonJS `require()`. The call graph is therefore sparser
than Python's (~0.5 edges/symbol on hono vs ~5 on django), so the BM25
and same-file legs carry more of the hybrid blend.

Two honesty notes, measured not guessed: (1) the hono result above is
one repo, mined by the same `verify` harness you can run on your own
repo — treat it as a smoke signal, not a benchmark; the five-repo
benchmark methodology has not been applied to TS yet. (2) the
`verify` sufficiency score is **not calibrated for TS**: on hono it
reported 100 for every case while measured recall ranged 50–100%, so
calibration mode currently has no discriminating power there — run
`--calibrate` and trust the recall numbers, not the score.

Without the extra installed, DiffContext is exactly the Python-only
tool: no behavior change, no warnings. Vendored/static JS inside Python
repos (django's admin jquery, for example) is excluded by an
adapter-level policy (`static/`, `vendor/`, `*.min.*`, colocated
`*.test.ts`/`*.spec.ts`), so installing the extra does not pollute
existing Python indexes.

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
- **Absolute recall at starvation budgets is low for everyone.** At 1,000
  tokens, grep manages 0.08 and DiffContext 0.12 — almost nothing fits in
  1k tokens, and the meta header says so rather than pretending otherwise.

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

## Verify: sufficiency, test cases, calibration

`compile` says "here is relevant context." `verify` answers the harder
question — *is it sufficient, and how would you know?*

```bash
# Structural sufficiency report for a change (CI-gateable exit code)
diffcontext verify --ref HEAD~1

# Grade retrieval against expectations YOU know to be true about your repo
diffcontext verify --cases cases.json

# Mine real test cases from git co-change history, then check whether the
# sufficiency score actually tracks measured recall on this repo
diffcontext verify --from-history 30 --calibrate
```

The score is a structural proxy (direct-neighbor closure, budget-cut
pressure, graph confidence, parse health), not a guarantee — and the
calibration mode says so out loud when the proxy doesn't track reality on
your repo. Case file format, methodology, and the honesty contract:
[docs/VERIFY.md](docs/VERIFY.md).

## Testing

```bash
python3 -m pytest tests/ -q      # 103 tests, self-contained, <3s
```

## Roadmap

Ordered by measured impact (see the failure taxonomy above):

1. **Adaptive blend** — up-weight BM25 when graph confidence is low
2. **Override edges** — link same-named methods across class hierarchies
3. **Git co-change history as a fourth signal** — the only known path past
   the cross-subsystem ceiling
4. **Chain-complete budgeting** — finish one causal chain deeply before
   spreading breadth across many symbols (evidence: case studies where the
   right function was retrieved but its explanatory dependency was cut)
5. ~~**Calibrated confidence scores**~~ — shipped as `diffcontext verify`
   (see [docs/VERIFY.md](docs/VERIFY.md)); next step is learned per-repo
   component weights fit on accumulated case results
6. ~~**TypeScript support**~~ — shipped experimental as the
   `[typescript]` extra (tree-sitter adapter; see
   [Language support](#language-support)). Next steps, in order of
   measured need: type-annotation-based receiver resolution (the graph's
   sparseness is the dominant gap), tsconfig path aliases, applying the
   full five-repo benchmark methodology to TS repos, and TS-aware
   sufficiency calibration. The adapter interface
   (`diffcontext/languages/`) is the template for Go/Rust/Java.

Longer-form planning notes: [docs/PLAN.md](docs/PLAN.md).

## License

MIT
