# Benchmarks — does it actually work? (measured, not claimed)

Headline numbers and reproduction commands. Full methodology —
distinct-commit sampling, four baselines, bootstrap confidence intervals,
budget sweep, hand-audited failure taxonomy — in
[benchmarks/EVAL_V2_REPORT.md](../benchmarks/EVAL_V2_REPORT.md).

## Per-signal ablation

Each retrieval signal benchmarked alone against real commit history:

| Signal | Hit | Recall | Where it's blind |
|---|---|---|---|
| Call graph alone | 0.748 | 0.558 | Related code that never calls yours |
| BM25 keywords alone | 0.822 | 0.619 | Structure; ranks lexical noise |
| Same file alone | 0.693 | 0.506 | Anything cross-file |
| **Hybrid (this product)** | **0.856** | **0.690** | See [known limitations](#known-limitations-measured-not-guessed) |

## Against real developer behavior, across repos

The core benchmark asks: *a developer changed these functions together in
one commit; shown one, can the tool find the others?* Headline
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
[benchmarks/EVAL_V2_REPORT.md](../benchmarks/EVAL_V2_REPORT.md).

## Head-to-head vs grep, at identical token budgets

The question that actually matters for an agent loop: *given the same
context window, does this beat what a developer does by hand?* 30 real
co-change queries from black's history; recall of the true co-change
partners inside the packed window
([benchmarks/budget_head2head.py](../benchmarks/budget_head2head.py)):

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

## Quality can't silently regress

[benchmarks/check_regression.py](../benchmarks/check_regression.py)
enforces frozen hit/recall floors and runs in CI on every push. If a
change to the heuristics drops retrieval quality below the floors, the
build fails.

## Reproduce it yourself

```bash
pip install rank-bm25                          # benchmark-only dependency
python benchmarks/benchmark_runner.py --clone  # clone the five eval repos
python benchmarks/eval_v2_hardened.py          # full run (~10 min)
python benchmarks/budget_head2head.py benchmark_repos/black   # grep head-to-head
python benchmarks/check_regression.py          # the CI quality gate (~1 min)
```

Or skip our repos entirely and grade DiffContext against **your** repo's
real history: `diffcontext verify --from-history 20 --calibrate` — see
[VERIFY.md](VERIFY.md).

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
