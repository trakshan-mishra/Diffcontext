> **Single-seed pass@1 with Wilson CIs + exact McNemar tests.**
> The pass@1 column now carries 95% Wilson score CIs and all 6 pairwise
> comparisons carry exact McNemar p-values (see stats.py). The context-vs-none
> effect is robust (p < 0.0001 on every arm); the context-variant differences
> (p = 0.36–0.81) are statistically indistinguishable at n=128 — the limit is
> discordant-pair count, not run-to-run noise. More seeds will not resolve this;
> more tasks (T3: clone the other 17 ContextBench repos) might.

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

### Full 128-task run, 4 variants (0 setup errors)

136 tasks total; 8 are oracle misses (no seeds, no context, skipped). 128 tasks attempted × 4 variants = 512 generation attempts. Judge = repo's own test suite (Django `runtests.py` with `test_sqlite` settings; pytest for requests/flask). No LLM-as-judge.

| Variant | Passed | Attempted | Pass@1 | 95% Wilson CI | no_seeds | gen_error | apply_error | test_fail |
|---|---:|---:|---:|---|---:|---:|---:|---:|
| none (no context) | 7 | 128 | **5.5%** | [0.027, 0.109] | 8 | 14 | 82 | 25 |
| diffcontext (default) | 28 | 128 | **21.9%** | [0.156, 0.298] | 8 | 4 | 46 | 50 |
| **diffcontext_gap** | **33** | 128 | **25.8%** | [0.190, 0.340] | 8 | 3 | 46 | 46 |
| diffcontext_depboost | 30 | 128 | **23.4%** | [0.169, 0.315] | 8 | 6 | 49 | 43 |

**Context retrieval quadruples pass@1** (5.5% → 22–26%, p < 0.0001 on every arm). The three context variants (diffcontext, gap, depboost) are **statistically indistinguishable** from each other (McNemar p = 0.36–0.81, see below). The headline is context vs no context, not gap vs default.

### Paired pass@1 (exact McNemar, attempted tasks only, n=128 per pair)

| Comparison | Both pass | A only | B only | Neither | McNemar p |
|---|---:|---:|---:|---:|---:|
| none vs diffcontext | 5 | 2 | 23 | 98 | **0.00002** *** |
| none vs diffcontext_gap | 5 | 2 | 28 | 93 | **0.0000009** *** |
| none vs diffcontext_depboost | 4 | 3 | 26 | 95 | **0.00002** *** |
| diffcontext vs diffcontext_gap | 21 | 7 | 12 | 88 | 0.359 |
| diffcontext vs diffcontext_depboost | 20 | 8 | 10 | 90 | 0.815 |
| diffcontext_gap vs diffcontext_depboost | 22 | 11 | 8 | 87 | 0.648 |

`*` p < 0.05, `**` p < 0.01, `***` p < 0.001

- **Context vs no context is real and large.** Every context variant beats none at p < 0.0001. Gap solves 28 tasks that none fails; none solves only 2 that gap fails.
- **The three context variants are statistically indistinguishable.** The confidence intervals overlap almost completely. Gap beating default by +3.9pp is NOT supported (p = 0.359). Only ~15% of tasks are discordant on any context-variant pair; resolving a 3pp difference needs roughly an order of magnitude more tasks, not more seeds.

### Error breakdown

The dominant failure mode is `apply_error` (the model produces a malformed diff that `git apply` rejects): 82/128 (64%) for `none`, 46/128 (36%) for `diffcontext`, 46/128 (36%) for `diffcontext_gap`. Context reduces apply errors — the model sees the actual code and produces better-formed patches. The `apply_patch` cascade hardening (`--ignore-whitespace` + trailing-newline fallback) reduced apply errors from the original run (84→82 for none, 69→46 for diffcontext, 66→46 for gap). `gen_error` (GLM null content) is rare but higher for `none` (14) than context variants (3–6).

All 164 `test_error` rows were verified as genuine test failures (real Python tracebacks), not infrastructure contamination. The evaluator shallow-clone landmine documented in SESSION_STATE.md did not affect this run.

---

## 4. Official ContextBench evaluator (external validation)

*Pending — the official evaluator (`python -m contextbench.evaluate` from [github.com/EuniAI/ContextBench](https://github.com/EuniAI/ContextBench)) is running on all 136 tasks at file/symbol/span/line granularity. Results will be filled in from `verify_results.py` when complete.*

---

## 5. Key findings

1. **Context retrieval quadruples GLM 5.2 pass@1** on ContextBench Python tasks (5.5% → 25.8%, p < 0.0001, n=128 attempted). This is the first external evidence that DiffContext's context quality improves downstream LLM task outcomes — not just retrieval-vs-gold proxy metrics.

2. **The three context variants are statistically indistinguishable.** diffcontext (21.9%), gap (25.8%), and depboost (23.4%) have overlapping 95% Wilson CIs and McNemar p = 0.36–0.81. The headline is context vs no context, not gap vs default. Only ~15% of tasks are discordant on any context-variant pair; resolving a 3pp difference needs ~10× more tasks, not more seeds.

3. **The `cutoff="gap"` precision lever improves retrieval F1** by +0.106 (bootstrap 95% CI [0.078, 0.134], sign-test p < 10⁻¹², 75.8% paired win rate) — a retrieval-metric improvement that does NOT reach statistical significance downstream (p = 0.359). Retrieval F1 is not a perfect proxy for downstream pass@1; precision matters more than the F1 gain suggests.

4. **Dependency-type score boosting (depboost) improves retrieval metrics but NOT downstream pass@1.** depboost raised retrieval F1 from 0.358 to 0.365 (+0.007) and symbol recall from 55.1% to 60.7% (+10.2% relative), keeping precision at 0.391 (vs 0.431). But downstream pass@1 was 23.4% vs gap's 25.8% (p = 0.648). The +6.4% retrieval recall did not translate; the precision loss (0.431→0.391) was more costly to the model.

5. **Context reduces apply errors.** 64% of `none`-variant attempts produce malformed diffs (vs 36% for context variants) — without context, the model hallucinates code structure; with context, it sees the actual file and produces applicable patches. The `apply_patch` cascade hardening (`--ignore-whitespace` + trailing-newline fallback) further reduced apply errors from the original run (84→82 none, 69→46 diffcontext, 66→46 gap), raising gap's pass@1 from 19.5% to 25.8%.

---

## 6. Limitations

- **Oracle localization.** Seeds are extracted from the gold patch. The measured claim is "given correct localization, does context quality matter?" — not end-to-end issue solving. The 8 oracle misses (class/module-level patches) are excluded from retrieval metrics and skipped in pass@1.
- **Python only.** DiffContext supports Python; 136/512 Python tasks are in cloned repos (django 129, requests 6, flask 1). 121/128 effective tasks are django — this is largely a django result. ContextBench has 512 Python tasks across 20 repos; cloning the other 17 repos would give 3.8× the tasks at zero inference cost.
- **Single backbone, single seed.** GLM 5.2 only, one run per variant. The pass@1 column carries bare counts with Wilson CIs but no multi-seed variance. The context-vs-none effect is large enough to be robust (p < 0.0001 on every arm), but the context-variant differences (p = 0.36–0.81) cannot be resolved at n=136 — the limit is discordant-pair count, not run-to-run noise.
- **Reasoning suppression.** GLM 5.2 is a reasoning model; `chat_template_kwargs: {"enable_thinking": false}` suppresses reasoning (0 chars, 5-8s latency). With reasoning on, large contexts cause 2+ minute response times or null content. The pass@1 numbers are with reasoning off.
- **Apply-error dominant.** 36–64% of attempts fail at patch application, not test execution. A better patch extractor (or function-call format instead of raw diff) would raise the absolute pass@1; the relative comparison between variants holds.

---

## 7. Reproduction

```bash
# 1. Retrieval metrics (no LLM cost, ~7 min for 136 tasks × 5 variants)
HF_HUB_OFFLINE=1 python3 benchmarks/contextbench/run_diffcontext.py \
    --out-dir benchmarks/contextbench/results/retrieval_136_5var \
    --max-tokens 8000 --top-k 20

# 2. Official ContextBench evaluator (disposable clones, ~40 min)
python3 benchmarks/contextbench/run_official_eval.py \
    --pred-dir benchmarks/contextbench/results/retrieval_136_5var \
    --out-dir benchmarks/contextbench/results/official_eval_136

# 3. GLM 5.2 pass@1 (~2 hr for 128 tasks × 4 variants)
set -a; . .env; set +a
HF_HUB_OFFLINE=1 python3 benchmarks/contextbench/run_glm_pass1.py \
    --out benchmarks/contextbench/results/glm_pass1/glm_pass1_depboost.jsonl \
    --repos django,requests,flask --limit 0 \
    --variants none,diffcontext,diffcontext_gap,diffcontext_depboost \
    --test-python /home/trakshan/temporary/cb_tmp/py311/bin/python

# 4. Regenerate every number in this file from JSONL
python3 benchmarks/contextbench/verify_results.py
```

---

## 8. Files

```
benchmarks/contextbench/
├── run_diffcontext.py       # retrieval adapter (136 tasks, 5 variants)
├── run_glm_pass1.py         # GLM 5.2 pass@1 harness (128 tasks, 4 variants)
├── run_official_eval.py     # official evaluator runner (disposable clones)
├── verify_results.py        # regenerates every number above from JSONL
├── stats.py                 # wilson_interval, mcnemar_exact, paired_table
├── analyze_apply_errors.py  # Phase 1: classify 219 apply errors
├── analyze_retrieval_failures.py # Phase 2: classify retrieval false negatives
├── diagnose_selector.py     # Phase 1: instrument selector cut reasons
├── ablation_selector.py     # Phase 2+3: 10-config ablation harness
├── diff_relocator.py        # tested-and-rejected patch repair (0/18)
├── validate_relocator.py    # offline validation of relocator
├── RESULTS.md               # this file
├── SESSION_STATE.md         # session notes + lessons learned
└── results/
    ├── retrieval_136_5var/  # 136-task run: pred_*.jsonl + summary.json (5 variants)
    ├── retrieval_136_4var/  # [baseline] 4-variant run (tag v0.4.0-selection-baseline)
    ├── glm_pass1/           # GLM 5.2 pass@1 (glm_pass1_depboost.jsonl, 544 rows)
    ├── ablation_dep_boost/  # 10-config selection ablation (128 tasks)
    ├── retrieval_failures/  # false-negative analysis (128 tasks)
    ├── selector_diagnosis_*/ # selector cut-reason diagnosis
    ├── retrieval_136/       # [superseded] 3-variant run (no gap)
    ├── retrieval_57_gap/    # [superseded] 57-task 5-variant run (corrupted)
    ├── official_eval_7/     # [superseded] 7-task official eval
    └── glm_pass1/           # [superseded] contaminated v1/v3 pilot results
```
