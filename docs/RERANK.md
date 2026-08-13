# Stage-2 reranking — measurement report

Status: **implemented, validated, opt-in and default-OFF.**
Date: 2026-08-08. Branch `eval/rigor-pass`.

Every number here comes from a committed artifact. Reproduce with:

```bash
python -m benchmarks.rerank.mine  --repos django click flask httpx pydantic \
                                          black requests rich starlette --target 100
python -m benchmarks.rerank.train --mode all --objective pairwise --emit
```

Artifacts, following this repo's convention that bulk working data is ignored
and summaries are committed:

| artifact | in git | what it backs |
|---|---|---|
| `diffcontext/rerank/weights.json` | yes (3.4 KB) | the shipped model |
| `benchmarks/results/rerank/validation.json` | yes (18 KB) | every number in §1, §2 |
| `benchmarks/results/rerank/mining_summary.json` | yes (4 KB) | the table in §3 |
| `<repo>.npz`, `<repo>.meta.json` | no (18 MB) | regenerable working data |

---

## 1. Headline

The reranker produces a **small, reproducible, statistically significant**
precision gain over the shipped stage-1 blend. It is **well short of the
oracle ceiling** and short of the gain the implementation brief predicted.

| protocol | P@10 rerank / stage-1 | Δ | Holm p | verdict |
|---|---|---|---|---|
| LORO (cross-repo, 5 folds) | 0.231 / 0.216 | **+0.015** | 1.2e-11 | gain |
| Temporal, within-repo only | 0.238 / 0.231 | +0.007 | 0.755 | **null (underpowered)** |
| Temporal + cross-repo | 0.258 / 0.231 | **+0.027** | 6.0e-08 | gain |
| Frozen (never tuned on) | 0.230 / 0.225 | **+0.005** | 0.0225 | gain |

Recall moves the same direction: LORO r@20 0.658 vs 0.630, frozen r@20
0.702 vs 0.674. **No fold is below the shipped baseline on recall**, so the
usual precision/recall trade is not what is happening here — both improve, both
by a little.

### The flip side, stated plainly

The oracle ceiling on these same case sets is **P@10 = 0.401** against a
stage-1 baseline of **0.215** — an available gap of **+0.186**. The reranker
captures **+0.015 to +0.027 of it, i.e. 8–15%**. The brief predicted
learning-to-rank would capture 40–60% and land at P@10 ≈ 0.25–0.30. Only the
temporal+cross-repo condition (0.258) reaches that band; LORO and frozen
validation land at 0.23.

**The gate as written in the brief does not discriminate.** It asks for
held-out P@10 > 0.20 — but the shipped stage-1 baseline already scores 0.216
on the identical case set. The baseline passes the gate. The paired delta,
not the absolute level, is the number that carries information here.

---

## 2. The temporal result, and why the first reading was wrong

The plain within-repo temporal split (train on a repo's older 70% of commits,
test on its newer 30%) shows **+0.007 P@10, p = 0.755 — nothing**. Read alone,
that says the cross-repo gain does not survive forward-in-time evaluation.

That reading is wrong, and the design is what confounded it: this split trains
on ~200 queries from a single repo, roughly **one sixth** of LORO's training
volume, and tests on 474 pooled queries instead of 1572.

`--mode temporal_loro` separates the two causes: train on the other four repos
**in full** *plus* the held-out repo's older commits, then test only on its
newer commits. Training volume now matches LORO; only the test set is
forward-in-time. Result: **+0.027 P@10 (p = 6.0e-08)**, larger than LORO's own
estimate.

**Conclusion: the null was a power artifact, not a temporal-transfer failure.**
The gain does survive forward-in-time evaluation. The plain temporal number is
kept in the table because it is what the brief's protocol step 2 literally
specifies, and because a reader who runs only that mode will see a null and
should know why.

---

## 3. What the pool can and cannot do

Stage 1 truncated to top-100 is the reranker's universe. Measured per repo:

| repo | queries | rows | positive rate | rerankable | stage-1 r@100 |
|---|---|---|---|---|---|
| django | 298 | 29,800 | 3.29% | 94.9% | 0.827 |
| click | 355 | 35,500 | 4.54% | 97.5% | 0.838 |
| flask | 243 | 24,300 | 4.00% | 97.6% | 0.826 |
| httpx | 328 | 32,800 | 5.27% | 97.6% | 0.823 |
| pydantic | 348 | 34,800 | 4.25% | 92.8% | 0.751 |
| black | 334 | 33,400 | 4.74% | 98.2% | 0.840 |
| requests | 42 | 4,200 | 2.50% | 100.0% | 0.874 |
| rich | 279 | 27,900 | 3.60% | 92.1% | 0.848 |
| starlette | 317 | 31,700 | 4.04% | 97.2% | 0.848 |
| **total** | **2,544** | **254,400** | | | |

"Rerankable" = the query has at least one true co-change inside the top-100.
The other **2.4–7.9%** are unreachable by *any* reranker over this pool; they
are dropped from training and reported here rather than silently absorbed.
All metrics in §1 are computed on the rerankable subset, so they overstate
whole-corpus performance by roughly the reachable fraction (~3–5%). Both arms
are affected identically, so the paired deltas are unaffected.

This reproduces the brief's claim: r@100 ≈ 0.83, positive rate 3–6%,
~1.5k–3k queries. `requests` r@100 = 0.874 matches the brief's table exactly.

---

## 4. Why the gain is small

An ablation over the LORO folds (pooled P@10):

| feature subset | P@10 |
|---|---|
| shipped stage-1 (baseline) | 0.2162 |
| the 8 "stage-1-like" features only | 0.2183 |
| the 13 genuinely new features only | 0.2108 |
| all 21 | 0.2288 |

The new features **alone underperform the baseline**, and add ~+0.013 only in
combination. That is the signature of a feature set that mostly re-derives what
stage 1 already encodes — hops, BM25, same-file — rather than contributing an
independent signal. The oracle gap is real and large; these 22 features are not
the instrument that closes it.

Learned coefficients (standardized, pairwise objective, λ=0.03):

```
log_cand_tokens     +0.340     import_overlap      +0.190
inv_hop_undirected  +0.288     inv_bm25_rank       +0.171
body_token_overlap  +0.250     name_jaccard        +0.159
bm25_score          +0.242     dir_distance        -0.092
```

The strongest single feature is `log_cand_tokens` — a **size prior**. "Bigger
functions are likelier co-changes" is a base-rate effect, not a relevance
signal, and its dominance is itself evidence that the discriminative features
are weak.

### Two features were dead, one still is

`import_overlap` was constant 0.0 across the entire first training run.
`index._import_maps` is `None` on a graph-cache hit (documented, intended),
and the feature contract degrades a missing signal to 0.0 — so nothing errored
and the column quietly died. `benchmarks/rerank/mine.py:ensure_import_maps`
rebuilds them; the feature is now the 5th-strongest coefficient (+0.190).

`is_test` is **still** constant 0.0, legitimately: these repos index only
`src/`, so no test symbol is ever a candidate. It is retained for corpora that
do index tests, and it contributes exactly nothing here.

---

## 5. Calibration

The pairwise objective is scale- and shift-free, so its raw score is an
ordering, not a probability. Platt scaling on **out-of-fold** scores recovers
one without changing the ordering (a = 0.653 > 0 is monotone); (a, b) is folded
back into the coefficients, so `weights.json` stays a plain linear model.

**Reported ECE = 0.0028 — and that number is misleading.** It is mass-weighted,
and 146,790 of 157,200 rows fall in the lowest bin. The bins that a probability
cutoff would actually operate on are badly overconfident:

| predicted | empirical | n |
|---|---|---|
| 0.84 | 0.63 | 188 |
| 0.95 | 0.58 | 204 |

**Do not ship `cutoff="prob"` on this model yet.** A threshold in the 0.8–0.95
region would systematically over-keep. `cutoff="gap"` remains the default and
remains measured. Fixing this needs either isotonic regression or a
per-query-normalized score, and is the next calibration task.

---

## 6. Cost

Feature extraction on flask (354 symbols, 100 candidates/query, n=60 queries):

| | mean | p50 | p95 |
|---|---|---|---|
| cold (empty token cache) | 3.53 ms | 3.29 ms | 5.53 ms |
| warm (shared token cache) | 3.35 ms | 3.20 ms | 4.49 ms |

Inside the brief's 5 ms/query budget at the mean and p50; p95 exceeds it cold.
The token cache is index-scoped and shared across queries, so steady-state is
the warm row. Inference itself (22 multiply-adds + one `exp`) is negligible.

---

## 7. Constraint compliance

| constraint | status |
|---|---|
| $0 budget, no paid API | held — Phase 1 is fully offline, CPU only |
| No GPU, no torch/sentence-transformers | held — none imported |
| No sklearn | held — numpy + `scipy.optimize.L-BFGS-B`, analytic gradients |
| Package keeps `dependencies = []` | held — inference is `json` + `math`; `tests/test_rerank.py::test_inference_never_imports_numpy` runs a subprocess and asserts no third-party module lands in `sys.modules` (verified against a deliberate-leak sentinel) |
| Validate on repos not used for tuning | held — frozen set black/requests/rich/starlette never informed any decision, including λ selection |

Test suite: **214 passed, 3 skipped, 1.71 s** (was 191/2 before this work).

---

## 8. Product integration and remaining work

- **Integration:** `analyze_impact(..., rerank=True)` and the opt-in
  `diffcontext compile --rerank` / `diffcontext verify --rerank` paths now
  invoke the model. The reranker receives only the top 100 candidates from the
  existing stage-1 hybrid order — exactly its measured universe — and leaves
  all later candidates in their stage-1 order.
- **Score-curve preservation:** the reranker transfers the existing stage-1
  score curve to its learned order instead of exposing model probabilities as
  generic impact scores. This changes ranking identity without changing the
  semantics of the separately-benchmarked `--cutoff gap` policy.
- **Default:** remains off. `--rerank` is a precision-first option; run
  `diffcontext verify --from-history 50` both with and without it on the target
  repository before changing an integration's default.
- **`cutoff="prob"`** — still blocked on §5. Do not enable.
- **Phase 2** (downstream eval: `oracle` arm, ~200 tasks) — untouched.
- **Phase 3** (TYPE_CHECKING parser fix, score-aware fusion) — untouched.

## 9. Recommendation

Ship as **opt-in, default off**. The effect is real and survives both
cross-repo and forward-in-time validation, but it is
~1.5–2.7 precision points, not the 3× the oracle suggests is available. Before
investing further in this feature set, the ablation in §4 argues the next move
is a genuinely independent signal, not more features of the same kind — which
runs into the no-GPU constraint, and should be decided deliberately rather than
by momentum.
