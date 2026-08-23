# DiffContext

**Show an AI coding assistant only the code that matters for the change it is
making.**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue)](https://www.python.org)
[![CI](https://img.shields.io/github/actions/workflow/status/trakshan-mishra/Diffcontext/test.yml?branch=main)](https://github.com/trakshan-mishra/Diffcontext/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

DiffContext is a **context compiler for LLM coding agents**. Give it a Python
repository and a change — a git diff, a branch, or a single function name —
and it returns the small set of functions the model actually needs to make
that change safely: the callers that will break, the subclasses that override
it, the tests that cover it. It fits them to whatever token budget you have,
and it tells the model what it had to leave out.

It is built for people wiring LLMs into real codebases — agent loops, PR
review bots, CI checks — anywhere you have to decide what goes in the prompt
and the repository is far too large to send.

## The problem

Ask an assistant to change one function in a 50,000-line project and you have
three bad options: paste the whole repository (it does not fit, and models get
worse in very large contexts), paste just that one function (the model breaks
three callers it never saw), or grep for the name (grep cannot find the
subclass that overrides it, or the handler that receives it through
`functools.partial` — we measured grep's recall *plateauing* no matter how
much budget you give it).

DiffContext is the fourth option. Parse the repository once into a real
dependency graph, then for any change select the few functions that actually
matter and pack them into the smallest useful prompt.

```
git change ──► changed functions ──► hybrid retrieval ──► token budget ──► LLM-ready context
                                      graph ∪ BM25 ∪ file      top-k + tokens
```

## Install

```bash
pip install diffcontext
```

Zero runtime dependencies, Python 3.9+.

For MCP integration (Claude Code / Cursor / Windsurf):

```bash
pip install "diffcontext[mcp]"
```

See [docs/MCP.md](docs/MCP.md) for the server config.

From source for development:

```bash
git clone https://github.com/trakshan-mishra/Diffcontext.git
cd Diffcontext && pip install -e .
```

## Quick start

```bash
diffcontext index /path/to/project              # cold: seconds; warm: ~0.02s
diffcontext compile --ref HEAD~1 --max-tokens 8000
diffcontext verify --from-history 20 --calibrate
```

More commands: [USAGE.md](USAGE.md). Production recipes: [docs/USE_CASES.md](docs/USE_CASES.md).

## Don't trust our benchmarks — run yours (2 minutes)

`diffcontext verify --from-history 20 --calibrate` mines test cases from
*your* repo's git history and grades retrieval against them — and prints
**NULL RESULT** rather than a decorative number when the tool doesn't fit
your repo. Finding that out *is* the feature.

## Does it make the model better?

Yes — measured end to end, not by proxy. On 128 ContextBench Python tasks
judged by each repository's own test suite (no LLM-as-judge), **context
roughly quadruples pass@1: 5.5% → 25.8%**, exact McNemar p < 0.0001.

Two qualifiers, both in [`benchmarks/contextbench/RESULTS.md`](benchmarks/contextbench/RESULTS.md)
§6: **(a)** the seed functions given to every arm are **oracle** — extracted
from the gold patch — so this measures *"given correct localization, does
context quality matter?"*, not end-to-end issue solving (localization is
handed to every arm for free); **(b)** 121 of the 128 effective tasks are
django, so this is largely a django result.

The honest companion: the three context variants (default / gap / depboost)
are statistically **indistinguishable** from each other, p = 0.36–0.81. The
win is context versus no context — not this selector versus that one. Full
results: [`benchmarks/contextbench/RESULTS.md`](benchmarks/contextbench/RESULTS.md).

## What this is not

- **Not a code generator.** It selects and packs context; the model writes
  the code.
- **Not precision-first.** It casts a wide net — mean precision is under 0.1
  at the default top-k. Use `--cutoff gap` if you pay per token.
- **Not multi-language yet.** Python is fully supported. TypeScript/JS (ESM)
  is a working prototype; CommonJS is a measured failure mode.
- **Not a replacement for reading the code.** Static analysis has blind spots,
  itemized below and in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

## Retrieval quality (measured, not claimed)

Ground truth is mined from git history — *a developer changed these functions
together in one commit; shown one, does the tool find the others?* Measured on
**701 real commits across 9 Python repositories**, and re-run as a CI gate on
every push so quality cannot silently regress.

Per-commit hit / recall of real co-change partners, hybrid retrieval:

| | django | click | flask | httpx | pydantic | black* | requests* |
|---|---|---|---|---|---|---|---|
| Hit | 0.894 | 0.889 | 0.863 | 0.935 | 0.758 | 0.897 | 0.953 |
| Recall | 0.774 | 0.750 | 0.694 | 0.772 | 0.536 | 0.712 | 0.762 |

\* validation repos, never used for tuning. Full table across all 9 repos:
[benchmarks/README.md](benchmarks/README.md).

Head-to-head vs grep at identical token budgets, grep **plateaus** at
0.215 recall past 4k tokens while DiffContext reaches 0.576 at 8k
(2.7×). The honest flip side: mean precision is under 0.1 at the default
top-k — most retrieved symbols are supporting context, not the exact
co-change set. `--cutoff gap` cuts at the largest score drop for ~4×
precision at ~30% recall cost (co-change benchmark; 2.2× / ~14% on
ContextBench).

## I audited my own benchmark, and three of my claims lost

A 2026-07 pass attacked the *evaluation* instead of the tool. Three published
numbers did not survive:

- **Calibration** — the only citable number (r=0.274, n≈25) was measured on a
  polluted index. Re-measured clean at n=1,080 the legacy score gets
  **r=0.016 (p=0.60)**: no relationship at all. Fixed by shrinking toward
  "don't know" → **r=0.287 (p=0.0001)** — a ranking signal, not a probability.
- **Blend weights** — the shipped [0.5, 0.35, 0.15] failed leave-one-repo-out;
  every fold picked a less graph-heavy blend. Now [0.3, 0.5, 0.2].
- **Dense baseline** — a TF-IDF stand-in had overstated dense retrieval (0.664,
  beating BM25 5/5). The real MiniLM encoder scores 0.597 and beats BM25 only
  2/5. Two prior conclusions corrected on the record.

Full write-up: [docs/auditing-my-own-benchmark.md](docs/auditing-my-own-benchmark.md)
· raw pass: [benchmarks/RIGOR_REPORT_2026-07.md](benchmarks/RIGOR_REPORT_2026-07.md).

## Use as a library

```python
from diffcontext.pipeline import index_repository, analyze_impact, compile

idx = index_repository("/path/to/repo")
impact = analyze_impact(idx, ["./src/auth.py:validate_jwt"])
ctx = compile(idx, impact, max_tokens=8000, top_k=20)
print(ctx.text)  # paste-ready, meta-header discloses what was dropped
```

Incremental API (`idx.update([...])`), structured output, pluggable tokenizer:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Language support

| Language | Status | Retrieval quality |
|---|---|---|
| Python | **Full** | Benchmarked: 701 commits, 5 repos + 4 validation repos |
| TypeScript / JS (ESM) | **Prototype** | Mean recall **0–68% depending on code style** |
| JavaScript (CommonJS) | **Unsupported** | Measured **0.0%** on express — do not use |

## Known limitations (measured, not guessed)

Static analysis has a ceiling: thematic siblings with no call between them,
cross-subsystem conceptual links (all methods score **0/20**), and dynamic
dispatch are measured blind spots — itemized in
[docs/BENCHMARKS.md](docs/BENCHMARKS.md). When in doubt:
`grep -rn "function_name(" --include="*.py" .` before fully trusting
"no callers found."

## More

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — pipeline, module map, agent API
- [docs/BENCHMARKS.md](docs/BENCHMARKS.md) — all numbers, downstream pass@1, limitations
- [docs/MCP.md](docs/MCP.md) — MCP server for Claude Code / Cursor / Windsurf
- [docs/ROADMAP.md](docs/ROADMAP.md) — prioritized plan with measured motivations
- [diffcontext-service/](diffcontext-service/) — FastAPI service + web UI
- [observability/](observability/) — retrieval pipeline tracing
- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, CI gates, adapter development

## License

MIT
