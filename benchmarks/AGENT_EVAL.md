# Agent evaluation layer

`benchmarks/agent_eval.py` — one command, one task unit, seven metrics, three
arms.

Every other harness in `benchmarks/` measures one thing well and reports it in
its own units: `context_reduction.py` sizes the prompt, `eval_v2_hardened.py`
scores retrieval against co-change ground truth, `downstream/run_eval.py` runs
an LLM and lets the repo's own tests judge. Answering *"is retrieved context
worth it, and at what cost?"* meant joining three result files measured on
three different units. This layer measures them together.

## The unit

One **mined task**: a real commit whose own tests fail at the parent and pass
at the commit (`downstream/tasks.py`). That is the only unit that can carry an
agent-success number, because it is the only one with an executable,
repo-native judge — no LLM grading, no proxy metric.

## The arms

| arm | what it is |
| --- | --- |
| `fullrepo` | no retrieval — every symbol in the repo, source order, packed until the budget runs out |
| `semantic` | dense retrieval over function sources (`sentence-transformers/all-MiniLM-L6-v2`) — ordinary RAG-for-code |
| `diffcontext` | the dependency-graph hybrid |

`bm25`, `samefile`, `diffcontext_gap`, and `none` remain available via
`--arms`. Held fixed across arms: model, system prompt, user prompt, token
budget, seeds. The only varying bytes are the context block.

## The metrics

| metric | definition |
| --- | --- |
| `full_repo_tokens` | the whole repo rendered in the arms' own block format, no budget |
| `context_tokens` | what the arm actually put in the prompt |
| `token_reduction_pct` | `1 - context_tokens / full_repo_tokens` |
| `context_precision` | of what was shown, the fraction that was needed |
| `context_recall` | of what was needed, the fraction that was shown |
| `agent_success` | did the LLM's patch make the repo's tests pass |
| `retrieval_ms` / `corpus_build_ms` / `index_ms` / `generation_ms` | latency, split by what actually causes it |

Token counts use the renderer's own estimator (`len/4 * 1.2`) applied to the
rendered format, so `full_repo_tokens` is literally "this repo, in the same
format the arms emit, with no budget." Every reduction is therefore a ratio of
two like-for-like measurements. They are estimates, not tiktoken output — the
ratio is the claim, not the absolute counts.

Note this estimator differs from `context_reduction.py`'s (`len // 4`). The
numbers here are not interchangeable with that harness's; each is internally
consistent.

## Read this before quoting precision or recall

These tasks use **oracle localization**: every arm is seeded with the functions
the gold patch modifies. That is deliberate — it isolates context quality from
localization ability — but it means the obvious relevance set has already been
handed to the model. Scored directly, recall would be undefined and precision
zero for every arm.

So retrieval quality is scored under **leave-one-out seeding**: for a task
whose gold patch touched symbols `S` (`|S| >= 2`), each `s` in `S` is held out
in turn, the arm is seeded with `S \ {s}`, and the relevant set is `{s}`.
Results are averaged over the folds.

Three consequences, all of which must travel with the numbers:

1. **Tasks with one seed are excluded, not zeroed.** They report
   `loo_eligible: false` and null precision/recall. Scoring them zero would
   drag every arm toward the floor in proportion to how many single-symbol
   commits a repo happens to contain. The report prints how many tasks
   qualified.
2. **The agent pass and the retrieval pass use different seedings.**
   `agent_success` runs on full oracle seeding; precision/recall come from the
   leave-one-out pass. Same arm, same task, *different prompt*. Precision and
   recall characterize the arm's retrieval; `agent_success` characterizes the
   arm's context. Do not read the former as a description of the prompt that
   produced the latter.
3. **Precision is scored over what survived the budget**, not over the ranked
   list. An arm that proposes a perfect ranking and then overflows the window
   gets no credit for the part the model never saw.

## Latency is split three ways, deliberately

Pooling these produces numbers that are wrong by orders of magnitude:

- `index_ms` — parsing and graph construction, shared by every arm.
- `corpus_build_ms` — the dense arm embedding the corpus. An **indexing** cost,
  paid once and amortized over every query against that corpus.
- `retrieval_ms` — per-query ranking, steady state.

The first measured semantic query on a cold process pays model load plus a
full-corpus forward pass. Measured naively that lands in `retrieval_ms` and
reports ~20,000 ms against DiffContext's ~26 ms — a 700x gap that is entirely
startup cost. Charged correctly, the steady-state comparison runs the *other*
way: dense similarity search is sub-millisecond, and DiffContext's graph
traversal is the slower of the two per query.

`warm_semantic()` pays the startup cost before anything is timed, and the
harness bills it to `corpus_build_ms` rather than discarding it — warming makes
the number honest by moving it to the right column, not by deleting it. The
report prints cold and steady-state separately and never averages them.

The embedding cache is keyed by a hash of each function's **source**, so a
sweep across N commits of one repo embeds each distinct body once instead of N
times. That is a legitimate optimization (a real deployment would cache too),
but it means `corpus_build_ms` after the first task is *incremental* cost.

## Measured, 2026-08-13

52 tasks across click, flask, requests, rich, starlette. Retrieval arms at an
8,000-token budget; `fullrepo` at 128,000. Agent success not yet run (needs an
API key — see below).

| arm | repo tokens | context tokens | reduction | precision | recall | ret ms/query |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `fullrepo` | 110,071 | 95,462 | 7.4% | 0.002 | 0.932 | 0.3 |
| `semantic` | 110,071 | 7,988 | 91.5% | 0.024 | 0.680 | 0.5 |
| `diffcontext` | 110,071 | 7,992 | 91.5% | **0.032** | **0.806** | 56.9 |

What holds up and what does not, stated separately:

- **Precision differences are significant.** Paired Wilcoxon, Holm-corrected:
  `diffcontext` > `semantic` (Δ+0.008, holm p=0.029), `diffcontext` >
  `fullrepo` (Δ+0.030, holm p=0.002), `semantic` > `fullrepo` (Δ+0.022,
  holm p=0.002).
- **The recall advantage is not.** `diffcontext` leads `semantic` by +0.126,
  but at n_eff=6 that is holm p=0.169 — suggestive, underpowered, and it must
  not be quoted as a result. Only 16 of 52 tasks are leave-one-out eligible;
  that is the binding constraint on this table, and more repos, not more
  samples, is the fix.
- **"Paste the whole repo" is not always available.** `fullrepo` overflowed a
  128k window on 11 of 52 tasks, showing 70.9% of the repo's symbols on those.
  Its recall of 0.932 is therefore not the 1.000 that "show everything" implies
  — on the largest repos it cannot show everything. Where it does fit, it costs
  ~12x the tokens of a retrieval arm for precision of 0.002.
- **DiffContext is the slowest arm per query** — 56.9 ms against dense
  retrieval's 0.5 ms, roughly 100x. It buys that back by not needing a corpus
  embedding pass (65.7 s cold, 486 ms/task incremental on this corpus), but at
  steady state on a warm index, dense retrieval is far faster. Reported this way
  deliberately: pooling the cold cost into per-query latency would have shown
  DiffContext winning latency by ~700x, which is false.

Reproduce:

```bash
for r in click flask requests rich starlette; do
  python benchmarks/agent_eval.py \
      --tasks benchmarks/downstream/tasks/$r.json --repo benchmark_repos/$r
done
python benchmarks/agent_eval.py --report benchmarks/results/agent_eval/*.jsonl
```

## Running it

```bash
# Retrieval + token + latency metrics. No API key, no cost.
python benchmarks/agent_eval.py \
    --tasks benchmarks/downstream/tasks/click.json --repo benchmark_repos/click

# Self-test the judge (free): gold must PASS every task on every arm,
# empty must FAIL every one. Run this before spending any API budget.
python benchmarks/agent_eval.py --tasks ... --repo ... --mock gold
python benchmarks/agent_eval.py --tasks ... --repo ... --mock empty

# Add agent success rate. Free-tier backends need only `requests`.
export GEMINI_API_KEY=...
python benchmarks/agent_eval.py --tasks ... --repo ... \
    --backend gemini --samples 3 --sleep 4 --tag gemini-flash

# The table.
python benchmarks/agent_eval.py --report benchmarks/results/agent_eval/*.jsonl
```

The semantic arm needs `sentence-transformers`. Without it the arm silently
falls back to a lexical TF-IDF approximation — so the harness records the
encoder on every row and the report refuses to present a fallback as a dense
baseline, printing a `LEXICAL FALLBACK` warning instead.

Runs are resumable and keyed by `(model, commit, arm, sample)`. Transient
infrastructure failures (429/5xx/network) are never recorded as a failed fix;
they are retried on the next run and excluded from the report.

## Disclosed limitations

Everything in `downstream/README.md` §"Disclosed limitations" applies unchanged
— oracle localization, contamination, task-family skew, power, stochasticity.
Additionally:

- **The `fullrepo` arm gets its own budget** (`--fullrepo-tokens`, default
  128,000). Capping "paste the whole codebase" at the retrieval arms' 8k would
  reduce it to "the alphabetically-first few files", which is a strawman rather
  than a baseline. When the repo still does not fit, that is the result, and
  the report says so along with what fraction of the repo's symbols the arm
  managed to show.
- **A truncated arm's reduction number is a budget artifact**, not a selection
  result. The report flags which arms were truncated on how many rows.
- **`gold_rank` averages only over folds where the symbol was found.**
  Averaging in a sentinel for the misses would let a shallow arm that finds one
  symbol at rank 1 beat a deep arm that finds four at rank 8. Read it with
  recall, never alone.
- **The paired unit for `agent_success` is `(model, commit)`**, not `commit`. A
  pair must never straddle two models, or model disagreement reads as arm
  discrimination. The report prints a `discrimination:` line first — if no task
  separates the arms, the result set cannot support any claim in *either*
  direction, and the pass-rate table below it is not evidence.
- **The corpus is five repos because that is what mines cleanly here.**
  `pydantic` and `black` were attempted and yielded 0 validated tasks — their
  suites do not run green in this environment, so no commit can satisfy
  fail@state/pass@gold. `httpx` is empty for the same reason. Task mining is
  gated on a runnable test suite, which biases the corpus toward repos with
  low-friction test setups.
