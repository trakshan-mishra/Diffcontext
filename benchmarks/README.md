# DiffContext — Benchmark Results

**Static dependency analysis cuts the code you have to send an LLM by
77–99% (median 89%), while keeping 55–79% of the functions the developer
actually changed alongside.**

On Django — 9,164 functions, 1.2M tokens of source — a change to one
function compiles into a **9.5K-token prompt (99.2% smaller)** that still
contains **79%** of the functions that commit really touched. Index: 2.9s
cold, 96ms warm. Retrieval: 325ms.

Measured on **9 real Python repositories** and **701 real commits**, with
ground truth mined from git history: *a developer changed these functions
together in one commit; shown one, does the tool retrieve the others?*
Four of the nine repos (black, requests, rich, starlette) were never used
for tuning or weight selection.

---

## Results

Per-commit aggregate (a commit counts once, so a 200-function refactor
can't outvote 200 ordinary commits). Sorted by repository size.

| Repository | Total functions | Full-repo tokens | Retrieved functions | Retrieved tokens | Token reduction | Precision | Recall | Hit rate | Index (cold) | Index (warm) | Query | n commits |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| django | 9,164 | 1,201,611 | 53.5 | 9,484 | **99.2%** | 0.054 | 0.786 | 0.897 | 2.89s | 96ms | 325ms | 87 |
| pydantic | 1,827 | 368,635 | 26.3 | 9,433 | **97.4%** | 0.086 | 0.547 | 0.766 | 0.71s | 15ms | 89ms | 85 |
| rich* | 888 | 156,554 | 39.7 | 9,454 | **94.0%** | 0.070 | 0.763 | 0.845 | 0.36s | 9ms | 27ms | 91 |
| black* | 648 | 145,412 | 31.9 | 9,599 | **93.4%** | 0.083 | 0.711 | 0.897 | 0.76s | 11ms | 21ms | 85 |
| click | 517 | 87,948 | 39.2 | 9,653 | 89.0% | 0.076 | 0.750 | 0.889 | 0.13s | 5ms | 17ms | 95 |
| flask | 354 | 66,844 | 34.9 | 9,671 | 85.5% | 0.076 | 0.694 | 0.863 | 0.10s | 5ms | 10ms | 74 |
| starlette* | 484 | 55,424 | 65.5 | 9,194 | 83.4% | 0.057 | 0.782 | 0.935 | 0.15s | 5ms | 8ms | 85 |
| httpx | 434 | 56,670 | 54.0 | 9,813 | 82.7% | 0.100 | 0.772 | 0.935 | 0.10s | 5ms | 8ms | 83 |
| requests*† | 248 | 42,663 | 46.5 | 9,712 | 77.2% | 0.099 | 0.762 | 0.953 | 0.08s | 4ms | 6ms | 16 |

\* held-out validation repo — never used for tuning or weight selection.
† only 16 usable commits; see [Known limitations](#known-limitations).

**Hit rate** = at least one true co-change partner made it into the
prompt. **Recall** = fraction of them that did. **Precision** = fraction
of retrieved functions that were true co-change partners.

---

## Read this before quoting the reduction number

The reduction percentage is **not** a retrieval-quality metric, and it
would be dishonest to present it as one.

Look at the "Retrieved tokens" column: every repo lands between 9,194 and
9,813 against a **10,000-token budget** — 92–98% budget utilization. The
compiler always fills the budget you give it. So:

```
token reduction ≈ 1 − (your budget ÷ repo size)
```

That is arithmetic, not intelligence. Any retriever that respects a token
budget produces the same reduction. **The reduction number tells you the
prompt fits. The recall number tells you it's still worth reading.** Quote
them together or not at all.

It also follows that reduction scales with repo size, which is why the
column runs from 77% (requests, 43K tokens) to 99% (django, 1.2M tokens).
The honest reading: **on repositories small enough to paste wholesale,
this tool's compression is not the point** — requests fits in any modern
context window. The compression matters on django, and on the private
monorepos that look like django.

### The comparison that *is* about quality

Holding the budget identical and varying only the retriever — 30 real
co-change queries from black's history
([budget_head2head.py](budget_head2head.py)):

| Token budget | grep-packing | DiffContext | |
|---|---|---|---|
| 1,000 | 0.083 | 0.122 | +47% |
| 2,000 | 0.145 | 0.282 | ~2× |
| 4,000 | 0.215 | 0.408 | ~2× |
| 8,000 | **0.215 (plateau)** | **0.576** | **2.7×** |

Same prompt size, 2.7× the recall. And note grep *plateaus* — past ~4K
tokens more budget buys it nothing, because name-matching cannot find a
co-change partner that never mentions the name.

---

## Precision is the honest weakness

Per-commit precision is **0.054–0.100**. Roughly 90–95% of what gets
retrieved is not in the ground-truth co-change set.

Most of it is structurally adjacent supporting context — callers,
callees, same-file siblings — which is usually what you *want* an LLM to
see before it edits a function. But if you pay per token, this, not
recall, is the product's real problem. Adjusting for known
incompleteness in the ground truth still leaves precision under 0.15
everywhere ([RIGOR_REPORT_2026-07.md](RIGOR_REPORT_2026-07.md) §2).

The measured lever, shipped as `--cutoff gap`: cut the ranking at the
largest relative score drop instead of a fixed top-k. Roughly **4× the
precision at 6–9 retrieved symbols, costing ~30% relative recall.** Not a
free lunch, so recall-first top-k stays the default.

---

## Before / after

A real change to `Flask.dispatch_request`, using the shipped CLI:

```bash
diffcontext compile --repo flask --changed ./src/flask/app.py:Flask.dispatch_request
```

**Before — paste the repo:** 354 functions, 67,148 tokens.

**After — 15 functions, 8,562 tokens (87.25% reduction).** What it chose,
with the tool's own scores:

```
CHANGED   Flask.dispatch_request                    112
          ├─ CALLERS  full_dispatch_request, finalize_request, handle_exception,
          │           handle_user_exception, log_exception, make_default_options_response
          └─ CALLEES  raise_routing_exception, make_default_options_response, ensure_sync,
                      handle_user_exception, handle_exception, log_exception, +2 more

IMPACTED  Flask.preprocess_request                   87
          Flask.handle_user_exception                80
          Flask.finalize_request                     80
          Flask.handle_exception                     79
          Flask.make_default_options_response        76
          Flask.full_dispatch_request                75
          Flask.log_exception                        73
          Flask.ensure_sync                          73
          Flask.handle_http_exception                73
          Flask.url_for                              72
          Flask.raise_routing_exception              71
          Flask.process_response                     69
          Flask.wsgi_app                             67
          Flask.async_to_sync                        59
```

Every one is a genuine structural neighbour of the request dispatch
cycle. No keyword search would reliably surface `ensure_sync` or
`async_to_sync` from the token "dispatch_request" — they are reached
through the call graph, not the name.

The output leads with a meta header that discloses what was **left out**:

```
=== DIFFCONTEXT META ===
Repo symbols total    : 354
Symbols IN context    : 15
Symbols DROPPED       : 339  ← you cannot see these
Graph edges total     : 2392
Graph confidence      : 100%  ✓
Note: graph confidence = STRUCTURAL completeness only. Static analysis
cannot see cross-subsystem conceptual coupling — such related code may
exist and not be listed anywhere above.

KNOWN MODULES (NOT IN CONTEXT — BLIND SPOTS):
  - ./src/flask/sessions.py (21 symbols)
  - ./src/flask/templating.py (15 symbols)
  ...

DROPPED SYMBOLS (339) — scored but cut by token budget:
  - ./src/flask/app.py:Flask.make_response  (score: 67)
  - ./src/flask/app.py:Flask.create_url_adapter  (score: 63)
  ...
```

This is the design bet: a model that knows *what it cannot see* fails
more safely than one that silently receives a truncated repo. Stress-
tested at a tight 2,000-token budget: 34% of ground truth made it into
context, 66% was explicitly disclosed as dropped, and **0% was silently
invisible** — every single miss appeared in the dropped manifest.

---

## Reproduce

```bash
pip install -e .
python benchmarks/context_reduction.py                 # the table above
python benchmarks/context_reduction.py --commits 20    # faster pass
python benchmarks/budget_head2head.py                  # vs grep
```

Results land in `benchmarks/results/context_reduction/reduction.json`,
pinned to the repo SHA each number was measured at.

**Or skip our repos and use yours** — two minutes:

```bash
diffcontext verify --from-history 20 --calibrate
```

This mines test cases from *your* git history and grades retrieval
against them. It prints **NULL RESULT** rather than a decorative number
when the tool doesn't fit your repo. Finding that out is the feature.

### Methodology

- **Ground truth:** distinct commits with ≥2 co-changed functions still
  alive at HEAD, mined from up to 6,000 commits of history.
- **Unit:** per-commit (mean within commit, then across commits). The
  per-symbol aggregate is also recorded in the JSON — see below for why
  the distinction matters.
- **Weights:** the shipped hybrid blend `[0.3, 0.5, 0.2]` (graph / BM25 /
  same-file), chosen by leave-one-repo-out validation.
- **Budget:** 10,000 tokens, matching the frozen eval_v1 baseline.
- **Token estimate:** `len(source) // 4`. Reduction is a ratio of two
  code measurements, so it is insensitive to tokenizer choice; absolute
  token counts are estimates, not tiktoken output.
- **Denominator:** sum of indexed function bodies. This *excludes*
  module-level code, comments outside functions, and non-Python files —
  so it is a conservative (smaller) denominator than a real
  cat-the-repo prompt, and understates the true reduction.

The recall numbers here reproduce the independently-run
[eval_v2_hardened](EVAL_V2_REPORT.md) table to within sampling noise
(django .786 vs .781, black .711 vs .712, requests .762 vs .762).

---

## Known limitations

**A single mechanical refactor can dominate a per-symbol average.** The
`requests` repo yields only 16 usable commits, and one of them — *"Add
inline types to Requests"* — changed **237 functions at once**. That one
commit contributes 84% of the repo's per-symbol rows, and it is not a
retrieval task at all: it's a type-annotation sweep with no semantic
relationship between the functions.

Aggregated per symbol, it makes the numbers meaningless in both
directions — precision inflates to 0.833 (when ground truth is nearly the
whole library, almost anything retrieved is "correct") while recall
collapses to 0.281 (237 functions do not fit in 10K tokens). Aggregated
per commit, the same data reads 0.099 / 0.762. **This is why the table
above is per-commit.** Both aggregations are recorded in the JSON.

**Other limitations, measured:**

- **Precision is low** (0.054–0.100) — see above.
- **pydantic is the weak repo** (recall 0.547): heavy dynamic dispatch
  and metaclass machinery that static analysis cannot follow.
- **Cross-subsystem coupling is a hard floor.** Django's
  cross-subsystem failure bucket stays at **0/20** even with git
  co-change history added. A settings flag and the unrelated code that
  reads it share no call edge, no lexical overlap, and often no commit.
- **Small repos don't need this.** Below ~50K tokens, paste the repo.
- **Python only** for these numbers. TypeScript/JavaScript is a working
  prototype ([LANG_ADAPTERS.md](../docs/LANG_ADAPTERS.md)).

---

## Further reading

| Document | What's in it |
|---|---|
| [EVAL_V2_REPORT.md](EVAL_V2_REPORT.md) | Full methodology, four baselines, bootstrap CIs, budget sweep, failure taxonomy |
| [RIGOR_REPORT_2026-07.md](RIGOR_REPORT_2026-07.md) | Leave-one-repo-out weight validation, true dense baseline, calibration at n=1080, paired significance tests |
| [docs/BENCHMARKS.md](../docs/BENCHMARKS.md) | Per-signal ablation, blend variants |
| [docs/VERIFY.md](../docs/VERIFY.md) | Running the benchmark against your own repository |
