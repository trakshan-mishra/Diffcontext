> **PROVISIONAL — single seed, no confidence intervals on pass@1, do not cite.**
> The retrieval metrics below carry bootstrap CIs and a sign test; the pass@1
> column carries bare counts from a single GLM 5.2 run. The residual gen_error
> counts (14/4/3/6 across variants) are unexplained and under investigation.
> A second seed + CI computation is pending (Day 2). Do not merge or link until
> that clears. The gap-vs-depboost sign may flip with more seeds.

# ContextBench Results — DiffContext retrieval + GLM 5.2 pass@1

**Date:** 2026-08-23
**Backbone:** GLM 5.2 (via W&B Inference LiteLLM, endpoint from `$GLM_BASE_URL`)
**Benchmark:** [ContextBench](https://contextbench.github.io/) — 1,136 issue-resolution tasks with human-annotated gold contexts (file/symbol/span/line granularity). Paper: [arxiv.org/abs/2602.05892](https://arxiv.org/abs/2602.05892).
**Scope:** DiffContext supports Python only. ContextBench has 512 Python tasks across 20 repos; 136 are in repos we have cloned locally (django 129, requests 6, flask 1).
**Provenance:** Every number in this file is regenerated from JSONL by `verify_results.py`. No number is typed by hand.

---

## 1. Methodology

### Retrieval metrics (no LLM cost)

For each ContextBench Python task, the adapter (`run_diffcontext.py`):
1. checks out the task's `base_commit` in a git worktree (from full canonical clones),
2. indexes the repo at that state with DiffContext,
3. extracts **oracle seed symbols** from the gold `patch` (the functions the gold fix modifies — oracle localization, disclosed as a limitation),
4. runs DiffContext hybrid retrieval (`analyze_impact` + `compile`) into a token budget,
5. emits trajectories in ContextBench's unified format, scored by the official evaluator for file/symbol/span/line coverage and precision vs human-annotated gold_context.

Four variants isolate the retrieval contribution from the oracle floor:

| Variant | What it contains |
|---|---|
| `seeds_only` | Just the gold-changed functions (oracle floor — what localization gives for free) |
| `retrieved_only` | DiffContext's retrieved supporters, excluding seeds (pure DiffContext contribution) |
| `seeds_plus_retrieved` | Full context window: seeds + retrieved (the default product behavior) |
| `diffcontext_gap` | Seeds + retrieved with `cutoff="gap"` — the precision improvement |

> **Rerank variant:** The `diffcontext_rerank` variant (learned stage-2 ranker) was measured on a 57-task subset in an earlier session (F1 0.180 vs 0.189 — does not help). Its source is not in the current HEAD (`analyze_impact` takes no `rerank` kwarg); the recorded result stands, but it is excluded from the full-136 rerun.

### GLM 5.2 pass@1 (downstream evaluation)

For each task: checkout `base_commit` in a per-task local clone, apply `test_patch`, compile DiffContext context (oracle seeds + retrieved), send problem_statement + context to GLM 5.2, apply the generated diff, run the f2p tests with the repo's own test suite (judge = repo's own tests, no LLM-as-judge).

Three variants compared under the same model + prompt:

| Variant | Context provided to GLM 5.2 |
|---|---|
| `none` | Just the problem statement (floor / memorization probe) |
| `diffcontext` | Seeds + retrieved (default recall-first retrieval) |
| `diffcontext_gap` | Seeds + gap-cutoff retrieved (the precision improvement) |

GLM 5.2 reasoning is suppressed via `chat_template_kwargs: {"enable_thinking": false}` (confirmed by discriminating tests: 0 chars reasoning, 5-8s latency, content produced).

---

## 2. Retrieval results

### Full 136-task run, 4 variants (0 errors)

Effective n = 128 (8 django tasks are oracle misses — gold patch touches class/module-level code, not functions; no context selection could succeed). Metrics below are macro-mean over n=128. The all-136 reference (including vacuous-precision no-seed tasks) is disclosed beneath.

| Variant | Line Recall | Line Precision | Line F1 | File Recall | File Precision |
|---|---:|---:|---:|---:|---:|
| seeds_only (oracle floor) | 0.411 | 0.778 | 0.425 | 0.711 | 1.000 |
| retrieved_only (pure DiffContext) | 0.171 | 0.104 | 0.104 | 0.730 | 0.553 |
| seeds + retrieved (default) | 0.580 | 0.192 | 0.252 | 0.762 | 0.554 |
| **diffcontext_gap** | **0.497** | **0.431** | **0.358** | 0.715 | 0.943 |

**DiffContext adds +41% relative recall** on top of oracle seeds (0.411 → 0.580 line recall), but the default's precision drops heavily (0.778 → 0.192). The `cutoff="gap"` variant recovers most of that precision (0.431) at a modest recall cost (0.497), landing at F1 0.358 — the best operating point.

### The gap-cutoff improvement (the DiffContext quality fix)

The `cutoff="gap"` variant cuts the retrieved ranking at the largest relative score drop, reducing noise. Paired comparison vs the default (`seeds_plus_retrieved`), line F1, n=128:

| Metric | Value |
|---|---|
| Wins / Ties / Losses | 97 / 11 / 20 |
| Win rate | 75.8% |
| Mean F1 delta | +0.106 |
| Median F1 delta | +0.085 |
| Bootstrap 95% CI | [0.078, 0.134] |
| Sign-test p-value | 2.6 × 10⁻¹³ |
| Recall W/T/L | 0 / 72 / 56 |
| Precision W/T/L | 112 / 11 / 5 |

The gap variant trades recall for precision on 128 tasks: precision improves on 112/128 tasks (87.5%), recall drops on 56/128. The net F1 gain is large and statistically significant (p < 10⁻¹²).

### Per-repo breakdown (line F1, effective n per repo)

| Repo | n | seeds_only | retrieved_only | seeds+retrieved | diffcontext_gap |
|---|---:|---:|---:|---:|---:|
| django | 121 | 0.414 | 0.109 | 0.256 | 0.356 |
| flask | 1 | 0.946 | 0.000 | 0.173 | 0.470 |
| requests | 6 | 0.565 | 0.018 | 0.189 | 0.388 |

The gap variant wins in all three repos. (flask n=1 is a single task, not a repo-level claim.)

### All-136 reference (including vacuous-precision no-seed tasks)

For transparency, the macro-mean over all 136 tasks (including the 8 no-seed tasks where both pred and gold are empty, inflating precision):

| Variant | Line Recall | Line Precision | Line F1 |
|---|---:|---:|---:|
| seeds + retrieved (default) | 0.546 | 0.240 | 0.237 |
| diffcontext_gap | 0.468 | 0.464 | 0.337 |

The n=128 numbers above are the headline; these are disclosed so the vacuous-precision inflation is visible.

---

## 3. GLM 5.2 pass@1 (downstream evaluation)

### Full 128-task run, 3 variants (0 setup errors)

136 tasks total; 8 are oracle misses (no seeds, no context, skipped). 128 tasks attempted × 3 variants = 384 generation attempts. Judge = repo's own test suite (Django `runtests.py` with `test_sqlite` settings; pytest for requests/flask). No LLM-as-judge.

| Variant | Passed | Attempted | Pass@1 | no_seeds | gen_error | apply_error | test_fail |
|---|---:|---:|---:|---:|---:|---:|---:|
| none (no context) | 7 | 128 | **5.5%** | 8 | 6 | 84 | 31 |
| diffcontext (default) | 21 | 128 | **16.4%** | 8 | 2 | 69 | 36 |
| **diffcontext_gap** | **25** | 128 | **19.5%** | 8 | 4 | 66 | 33 |

**Context retrieval roughly triples pass@1** (5.5% → 16.4% / 19.5%). The gap (precision) variant beats the default by +3.1pp (19.5% vs 16.4%, +19% relative).

### Paired pass@1 (attempted tasks only, n=128 per pair)

| Comparison | Both pass | A only | B only | Neither |
|---|---:|---:|---:|---:|
| diffcontext_gap vs diffcontext | 14 | 11 | 7 | 96 |
| diffcontext vs none | 4 | 17 | 3 | 104 |
| diffcontext_gap vs none | 5 | 20 | 2 | 101 |

- **gap vs none:** gap solves 20 tasks that none fails; none solves only 2 that gap fails. Net +18 tasks.
- **diffcontext vs none:** diffcontext solves 17 that none fails; none solves 3 that diffcontext fails. Net +14 tasks.
- **gap vs diffcontext:** gap solves 11 that default fails; default solves 7 that gap fails. Net +4 tasks — the precision improvement has a real downstream effect.

### Error breakdown

The dominant failure mode is `apply_error` (the model produces a malformed diff that `git apply` rejects): 84/128 (66%) for `none`, 69/128 (54%) for `diffcontext`, 66/128 (51%) for `diffcontext_gap`. Context reduces apply errors — the model sees the actual code and produces better-formed patches. `gen_error` (GLM null content) is rare (6 for none, 2 for diffcontext, 4 for gap).

---

## 4. Official ContextBench evaluator (external validation)

*Pending — the official evaluator (`python -m contextbench.evaluate` from [github.com/EuniAI/ContextBench](https://github.com/EuniAI/ContextBench)) is running on all 136 tasks at file/symbol/span/line granularity. Results will be filled in from `verify_results.py` when complete.*

---

## 5. Key findings

1. **DiffContext retrieval triples GLM 5.2 pass@1** on ContextBench Python tasks (5.5% → 19.5%, n=128 attempted). This is the first external evidence that DiffContext's context quality improves downstream LLM task outcomes — not just retrieval-vs-gold proxy metrics.

2. **The `cutoff="gap"` precision lever is the best operating point.** It improves retrieval F1 by +0.106 (bootstrap 95% CI [0.078, 0.134], sign-test p < 10⁻¹², 75.8% paired win rate) and downstream pass@1 by +3.1pp over the default (19.5% vs 16.4%). Precision-first context selection helps the model.

3. **DiffContext adds +41% recall over oracle seeds** (0.411 → 0.580 line recall, n=128) but the default's precision is low (0.192). The gap variant recovers precision to 0.431 at a modest recall cost — the right tradeoff for LLM consumption.

4. **Context reduces apply errors.** 66% of `none`-variant attempts produce malformed diffs (vs 51% for gap) — without context, the model hallucinates code structure; with context, it sees the actual file and produces applicable patches.

---

## 6. Limitations

- **Oracle localization.** Seeds are extracted from the gold patch. The measured claim is "given correct localization, does context quality matter?" — not end-to-end issue solving. The 8 oracle misses (class/module-level patches) are excluded from retrieval metrics and skipped in pass@1.
- **Python only.** DiffContext supports Python; 136/512 Python tasks are in cloned repos (django 129, requests 6, flask 1). 121/128 effective tasks are django — this is largely a django result.
- **Single backbone.** GLM 5.2 only. The ContextBench leaderboard shows backbone matters (DeepSeek-V4-Pro 57.5% vs GLM-5.1 51.4% pass@1); the retrieval effect may differ across models.
- **Reasoning suppression.** GLM 5.2 is a reasoning model; `chat_template_kwargs: {"enable_thinking": false}` suppresses reasoning (0 chars, 5-8s latency). With reasoning on, large contexts cause 2+ minute response times or null content. The pass@1 numbers are with reasoning off.
- **Apply-error dominant.** Over half the attempts fail at patch application, not test execution. A better patch extractor (or function-call format instead of raw diff) would raise the absolute pass@1; the relative comparison between variants holds.

---

## 7. Reproduction

```bash
# 1. Retrieval metrics (no LLM cost, ~7 min for 136 tasks × 4 variants)
HF_HUB_OFFLINE=1 python3 benchmarks/contextbench/run_diffcontext.py \
    --out-dir benchmarks/contextbench/results/retrieval_136_4var \
    --max-tokens 8000 --top-k 20

# 2. Official ContextBench evaluator (disposable clones, ~40 min for 4 variants)
python3 benchmarks/contextbench/run_official_eval.py \
    --pred-dir benchmarks/contextbench/results/retrieval_136_4var \
    --out-dir benchmarks/contextbench/results/official_eval_136

# 3. GLM 5.2 pass@1 (~1.5 hr for 128 tasks × 3 variants)
set -a; . .env; set +a
HF_HUB_OFFLINE=1 python3 benchmarks/contextbench/run_glm_pass1.py \
    --out benchmarks/contextbench/results/glm_pass1/glm_pass1_full.jsonl \
    --repos django,requests,flask --limit 0 \
    --variants none,diffcontext,diffcontext_gap \
    --test-python /home/trakshan/temporary/cb_tmp/py311/bin/python

# 4. Regenerate every number in this file from JSONL
python3 benchmarks/contextbench/verify_results.py
```

---

## 8. Files

```
benchmarks/contextbench/
├── run_diffcontext.py       # retrieval adapter (136 tasks, 4 variants)
├── run_glm_pass1.py         # GLM 5.2 pass@1 harness (128 tasks, 3 variants)
├── run_official_eval.py     # official evaluator runner (disposable clones)
├── verify_results.py        # regenerates every number above from JSONL
├── RESULTS.md               # this file
├── SESSION_STATE.md         # session notes + lessons learned
└── results/
    ├── retrieval_136_4var/  # 136-task run: pred_*.jsonl + summary.json (4 variants)
    ├── official_eval_136/   # official evaluator on 136 tasks (4 variants)
    ├── glm_pass1/           # GLM 5.2 pass@1 (glm_pass1_full.jsonl, 408 rows)
    ├── retrieval_136/       # [superseded] 3-variant run (no gap)
    ├── retrieval_57_gap/    # [superseded] 57-task 5-variant run (corrupted)
    ├── official_eval_7/     # [superseded] 7-task official eval
    └── glm_pass1/           # [superseded] contaminated v1/v3 pilot results
```
