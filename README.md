# DiffContext

**Find the code that matters for a change, and fit it into an LLM's context
window — automatically.**

```
git change ──► changed functions ──► hybrid retrieval ──► token budget ──► LLM-ready context
                                     graph ∪ BM25 ∪ file      top-k + tokens
```

- Zero runtime dependencies, Python 3.9+, `pip install -e .`
- **~2× the recall of grep at every token budget** on real co-change
  ground truth — at ~5-10% precision: a wide net with the right things in
  it, not a curated shortlist ([measured](docs/BENCHMARKS.md))
- Benchmarked on **423 real commits** across django, flask, click, httpx,
  pydantic, with independent validation on black and requests; retrieval
  quality is **CI-gated** on every push
- Output is **honest by construction**: a meta header discloses exactly
  which symbols were dropped, so the model knows what it cannot see
- Python is fully supported; TypeScript/JavaScript (ESM) is a working
  prototype via the optional `[typescript]` extra
  ([per-style results, including a 0% failure mode](docs/LANG_ADAPTERS.md))

## Why

Ask an AI assistant to change one function in a 50,000-line project and
you have three bad options: paste the whole repo (doesn't fit, and models
get worse in huge contexts), paste just the function (the model breaks
three callers it never saw), or grep for the name (grep can't find the
subclass that overrides it or the handler that receives it through
`functools.partial` — we measured grep's recall *plateauing* no matter the
budget). DiffContext is option 4: **understand the repository's structure
once, then, for any change, select the few functions that actually matter
and compile them into the smallest useful package for the model.**

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
parentheses, no arguments. More commands and options: [USAGE.md](USAGE.md).
Production recipes — agent loops, PR review, CI gates, and when *not* to
use this: [docs/USE_CASES.md](docs/USE_CASES.md).

## How it works

Parse every file once into an AST, resolve imports to real definitions,
and build a dependency graph (calls, inheritance, dispatch-sibling
overrides, decorators, function references passed as arguments). For a
change, walk the graph outward with distance-decayed scores, blend with
BM25 lexical similarity and same-file co-location (weights adapt: when
the graph has little to say, BM25 gets its weight) — plus, optionally,
git co-change history (`--with-history`), the only signal that can reach
related code with no structural or lexical connection. Then pack the top
candidates into your token budget — leading with a meta header that
discloses everything that was *dropped*. The whole index is cached content-addressed in SQLite, so
re-indexing an unchanged repo costs ~0.02s and a one-file edit re-parses
only that file.

Full walkthrough, module map, and the incremental agent-harness API:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Does it actually work? (measured, not claimed)

Per-commit hit / recall of real co-change partners, hybrid retrieval:

| | django | click | flask | httpx | pydantic | black* | requests* |
|---|---|---|---|---|---|---|---|
| Hit | 0.894 | 0.889 | 0.863 | 0.935 | 0.758 | 0.897 | 0.953 |
| Recall | 0.774 | 0.750 | 0.694 | 0.772 | 0.536 | 0.712 | 0.762 |

\* validation repos, never used for tuning or weight selection.

Head-to-head vs grep at identical token budgets, grep **plateaus** at
0.215 recall past 4k tokens while DiffContext reaches 0.576 at 8k
(2.7×). The honest flip side: mean precision is under 0.1 at the default
top-k selection — most retrieved symbols are supporting context (callers,
callees, siblings) rather than the exact co-change set. If you pay per
token, use **`--cutoff gap`**: it cuts the ranking at the largest score
drop instead of a fixed top-k — measured ~4× the precision at 6–9
symbols, costing ~30% relative recall. Top-k stays the recall-first
default; measure the tradeoff on your own repo with
`diffcontext verify --from-history 20 --cutoff gap`.

All tables, the per-signal ablation, the failure taxonomy, and
reproduction commands: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
A quality gate (`benchmarks/check_regression.py`) re-runs the benchmark
in CI and fails the build if retrieval quality drops.

**Don't trust our benchmarks — run yours (2 minutes):**
`diffcontext verify --from-history 20 --calibrate` mines test cases from
*your* repo's git history and grades retrieval against them — and prints
**NULL RESULT** rather than a decorative number when the tool doesn't fit
your repo. Finding that out *is* the feature. Case format and
methodology: [docs/VERIFY.md](docs/VERIFY.md).

## Use as a library

```python
from diffcontext import CoChangeIndex
from diffcontext.pipeline import index_repository, analyze_impact, compile

idx = index_repository("/path/to/repo")
impact = analyze_impact(
    idx, ["./src/auth.py:validate_jwt"],           # hybrid + adaptive by default
    history=CoChangeIndex("/path/to/repo"),        # optional 4th signal: git co-change
)
ctx = compile(idx, impact, max_tokens=8000, top_k=20)
# token-priced callers: compile(..., cutoff="gap") for the precision operating point

print(ctx.text)                      # paste-ready context with meta-header
print(ctx.dropped_symbols[:5])       # what the budget cut — never hidden
```

For agent loops there's an incremental API — `idx.update([...])`
re-parses only edited files (~0.5s vs full re-index), output is
structured per-item, and the token counter is pluggable. Details and
measured timings: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Language support

| Language | Status | Retrieval quality |
|---|---|---|
| Python | **Full** | Benchmarked: 423 commits, 5 repos + 2 validation repos |
| TypeScript / JS (ESM) | **Working prototype** (`pip install -e ".[typescript]"`) | Mean recall **0–68% depending on code style** — not one number |
| JavaScript (CommonJS) | **Effectively unsupported** | Measured **0.0%** on express — do not use on CJS repos |
| Go / Rust / Java / others | Not supported | Retrieves nothing |

Retrieval quality tracks code style, not language. Per-repo numbers,
what the TS adapter resolves, and a known-broken warning about the
`verify` score on TS: [docs/LANG_ADAPTERS.md](docs/LANG_ADAPTERS.md).
Without the extra installed, DiffContext is exactly the Python-only
tool — no behavior change.

## Known limitations (measured, not guessed)

Static analysis has a ceiling: thematic siblings with no call between
them, dispatch/override pairs, cross-subsystem conceptual links (all
methods score **0/20** there), and dynamic dispatch are measured blind
spots — itemized with the failure taxonomy in
[docs/BENCHMARKS.md](docs/BENCHMARKS.md). When in doubt:
`grep -rn "function_name(" --include="*.py" .` before fully trusting
"no callers found."

## Web service

A FastAPI service + single-file web UI lives in
[diffcontext-service/](diffcontext-service/): clone a GitHub repo by URL,
index it, and query blast radius / search / context over HTTP. The
[Dockerfile](Dockerfile) packages it for container platforms.

```bash
pip install fastapi uvicorn python-multipart aiofiles
uvicorn diffcontext-service.backend.main:app --port 8000
```

## Testing & contributing

```bash
python3 -m pytest tests/ -q      # self-contained, <3s
```

Setup, design constraints (zero-dep core), CI gates, and how to add a
language adapter: [CONTRIBUTING.md](CONTRIBUTING.md).

## Roadmap

The current prioritized plan, each item with its measured motivation, is
[docs/ROADMAP.md](docs/ROADMAP.md). Highlights still open: LLM-judged
downstream evaluation (the one metric family still missing — the main gap
between this and a main-track research paper, see
[docs/RESEARCH.md](docs/RESEARCH.md)), a dense fourth blend leg as an
opt-in extra (measured: the only significant recall gains on hard repos),
chain-complete budgeting, and CommonJS. Recently shipped from the
roadmap: dispatch-sibling override edges (the 0%-recall dispatch bucket),
git co-change history as a fourth signal (`CoChangeIndex` /
`--with-history`, benchmarked with test-commit exclusion), calibrated
confidence (`verify --save-calibration`), and TypeScript as a prototype
(remaining TS work in [docs/LANG_ADAPTERS.md](docs/LANG_ADAPTERS.md)).
Adaptive per-query blending shipped (on by default in `analyze_impact`,
opt out with `adaptive=False`) but measured as a null in the rigor pass —
no significant recall change on any benchmark repo.

## More

**Hosted docs: [diffcontext-docs.pages.dev](https://diffcontext-docs.pages.dev/)**

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — pipeline, module map, resolver capabilities, agent API
- [docs/BENCHMARKS.md](docs/BENCHMARKS.md) — all numbers, methodology links, limitations
- [docs/RESEARCH.md](docs/RESEARCH.md) — literature positioning and the gap list for a publishable paper
- [docs/VERIFY.md](docs/VERIFY.md) — sufficiency scoring, test cases, calibration
- [docs/LANG_ADAPTERS.md](docs/LANG_ADAPTERS.md) — TS/JS adapter detail and measured failure modes
- [benchmarks/RIGOR_REPORT_2026-07.md](benchmarks/RIGOR_REPORT_2026-07.md) — the 2026-07 methodology-hardening pass (LORO validation, true dense baseline, calibration at scale, GT validity)

## License

MIT
