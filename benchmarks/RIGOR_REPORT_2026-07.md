# Rigor pass, 2026-07-20 — closing the methodological holes

**Branch:** `eval/rigor-pass` · **Raw data:** `benchmarks/results/{calibration,gt_validity,loro,eval_v2}/`

Five known weaknesses of the published numbers, each now measured rather
than disclosed. Every claim below is reproducible from the scripts named,
on the pinned repo snapshots recorded inside each result file.

## 1. Calibration of the sufficiency score, at scale
*(script: `calibration_at_scale.py`; data: 1,080 mined cases across 9
Python repos — click, django, flask, httpx, pydantic + held-out black,
requests, rich, starlette — and 379 cases across 4 TS repos, all through
the real product path)*

**The previously citable number was wrong.** The only calibration on
record (Pearson r=0.274, n≈25) was measured on the polluted index and,
re-measured on clean indexes at scale, the legacy score formula shows
**r=0.016 (p=0.60)** — no relationship with measured recall at all. Root
cause: score components with zero observations behind them (no direct
neighbors, no outgoing edges, no ranker-relevant symbols) defaulted to a
perfect 1.0, cramming the distribution against 100 (Python μ=94.9 σ=9.9;
TypeScript μ=99.2 σ=5.0 — the "constant 100 on TS" bug was the same
defect made visible).

**Fix, then re-measure (evidence-aware score, shipped this branch):** the
score now shrinks toward 50 ("don't know") in proportion to missing
evidence. Same data, after:

| | legacy score | evidence-aware score |
|---|---|---|
| Python pooled (n=1080) | r=0.016 (p=0.60) | **r=0.287 (p=0.0001)** |
| — significant per-repo | — | 7/9 repos |
| TypeScript pooled (n=379) | r=0.025 (p=0.64) | **r=0.286 (p=0.0001)** |
| TS score spread | μ=99.2 σ=5.0 | μ=81.0 σ=17.3 |

CommonJS express (adapter extracts almost nothing) now scores ~55
low-evidence with measured recall 0.0, instead of a confident 100.

**Score/100 is still not a probability.** As a direct prediction of
recall it loses to predicting the training mean (Python: MAE 0.485 vs
0.345; ECE 0.446; Brier-on-pass 0.428 vs 0.243 base rate). The reliability
table shows why: mean recall is 0.07 in the 40–60 score band and 0.47 in
the 80–100 band — the score *ranks* risk usefully but its absolute values
over-promise. Claims must say "ranking signal", never "confidence",
unless calibrated per-repo (next).

**Learned weights: the honest split.** Re-fitting the four component
weights by least squares, validated leave-one-repo-out: held-out r ≈ 0
in every fold — **a null result; the components alone carry almost no
transferable signal** (they saturate near 1.0 on well-connected code).
Adding the runtime-available count features (`selected_count`,
`n_missing_direct`, `n_dropped_high`, `context_tokens`) changes the
answer: the extended fit beats the predict-the-mean baseline on held-out
MAE in **8/9 Python repos** (held-out r up to 0.65 on django and rich;
the exception is requests, whose mean recall of 0.11 no structural
feature predicts). Shipped accordingly: `verify --calibrate
--save-calibration` fits this predictor on ≥30 of the repo's own history
cases and later `verify` runs report a **calibrated recall estimate** in
place of a bare score.

## 2. Ground-truth validity, measured
*(script: `gt_validity.py`; product-hybrid retrieval; W = follow-up
window in commits; control = size-matched random draws)*

Threat: co-change GT is incomplete — a "false positive" may be changed in
the very next commit. Measured rate at which FPs appear among symbols
changed within the next W commits, vs random control:

| Repo | FP-future W=10 | random | lift | precision raw → adjusted |
|---|---|---|---|---|
| click | 0.042 | 0.024 | 1.7× | 0.076 → 0.114 |
| django | 0.005 | 0.001 | 4.0× | 0.049 → 0.054 |
| flask | 0.066 | 0.048 | 1.4× | 0.083 → 0.144 |
| httpx | 0.043 | 0.033 | 1.3× | 0.068 → 0.108 |
| pydantic | 0.008 | 0.005 | 1.5× | 0.092 → 0.100 |

Reading: GT incompleteness is real (systematically above random) but
**small** — crediting every near-future co-change still leaves precision
under 0.15 everywhere. "Precision is the product's real problem" survives
its own validity check; the benchmark's recall/hit conclusions are not
materially distorted by GT noise at these window sizes.

## 3. Leave-one-repo-out validation of the hybrid weights
*(script: `blend_loro.py`; data: `results/loro/loro_3leg.json`. Same
mining/budget/metrics as eval_v2; objective = per-commit mean recall;
p-values are paired sign-flip permutation tests on per-commit recall.)*

**The shipped weights (0.5/0.35/0.15) do not survive honest selection.**
Every LORO fold selects a less-graph-heavy, more-BM25-heavy blend
(graph 0.25–0.30, bm25 0.50–0.60, samefile 0.15–0.20; global best
[0.3, 0.5, 0.2]) — the same-repo tuning had over-weighted the graph.
**But the damage is small**: LORO-selected beats shipped on the held-out
repo in 4/5 folds by only +1.2 to +2.4 recall points, no fold reaching
p<0.05 (p = 0.06–0.79):

| held out | shipped | LORO-selected | self-best (oracle) | p |
|---|---|---|---|---|
| click | 0.734 | 0.746 | 0.754 | 0.063 |
| django | 0.766 | 0.761 | 0.788 | 0.787 |
| flask | 0.670 | 0.694 | 0.699 | 0.071 |
| httpx | 0.755 | 0.768 | 0.772 | 0.290 |
| pydantic | 0.519 | 0.536 | 0.564 | 0.060 |

On four repos never used for ANY selection (frozen global-best weights):
black 0.712 vs shipped 0.719, requests 0.762 vs 0.773, rich 0.760 vs
0.767, starlette 0.776 vs 0.765 — all within ±1.1 points, all n.s.
**Verdict: the headline conclusions were not an artifact of same-repo
tuning, but the honest weight recommendation is now [0.3, 0.5, 0.2], and
any paper table should cite the LORO column, not the self-tuned one.**

## 4. Adaptive (per-query dynamic) blend — null result
*(same script; graph weight scaled per query by min(1, n_graph/n0),
deficit redistributed; n0 ∈ {3,5,10,20,40} tuned LORO)*

Adaptivity conditioned on per-query graph evidence **adds nothing**: on
every fold, tuning picks the least-adaptive setting available and the
held-out metrics equal the static blend to four decimals (p=1.000), in
both the 3-leg and 4-leg grids. Queries with weak graph evidence are too
rare (or the blend already absorbs them) for per-query re-weighting to
matter. Reported as the null it is; "dynamic blending" should not be
claimed as a win.

## 5. True dense-embedding baseline (the §8 stand-in corrected)
*(script: `eval_v2_hardened.py` re-run, all six methods, pinned
snapshots, encoder `sentence-transformers/all-MiniLM-L6-v2` recorded in
every summary)*

Cross-repo mean (per-commit recall): hybrid **0.693** > bm25 0.624 >
**true dense 0.597** > graph-only 0.555 > samefile ~0.51.

**The TF-IDF stand-in had overstated dense retrieval.** §8's
`tfidf-cosine-approx` scored 0.664 mean recall and beat BM25 in 5/5
repos; the real MiniLM encoder scores 0.597 and beats BM25 in only 2/5.
Two §8 conclusions are corrected on the record:
- "the embedding baseline is the strongest single baseline" — **no**:
  with a real dense encoder, BM25 is again the strongest single method
  on these repos (3/5 repos, and on mean recall).
- "the hybrid loses to the embedding baseline on pydantic" — **no**:
  true dense scores 0.441 on pydantic vs hybrid 0.524. The graph-blind
  regime's winner is BM25 (0.531), by a statistically unremarkable
  margin over the hybrid.

Caveat stated with equal honesty: all-MiniLM-L6-v2 is a natural-language
encoder. A code-tuned embedding model could shift these numbers; no
claim is made beyond the encoder actually run.

**Where dense genuinely matters — the failure buckets.** Real dense is
the only method that cracks the "structural ceiling": cross_subsystem
5/20 (25%) where graph, BM25, and hybrid all score 0/20; thematic 11/20
(55%) and backend_dispatch 7/20 (35%), the best of any method. Dense
retrieval is not a better *general* retriever here, but it reaches
exactly where structure and keywords are blind — which motivates §6.

## 6. Four-leg blend (graph+bm25+samefile+dense): the first significant win
*(script: `blend_loro.py --dense`; data: `results/loro/loro_dense.json`)*

Adding the dense leg and selecting weights LORO (typical fold selection
[0.2, 0.35, 0.2, 0.25]) produces the only statistically significant
retrieval improvements measured today, precisely in the harder repos:

| held out | shipped 3-leg | LORO 4-leg | p |
|---|---|---|---|
| click | 0.734 | 0.741 | 0.628 |
| django | 0.766 | 0.781 | 0.415 |
| flask | 0.670 | **0.709** | **0.007** |
| httpx | 0.755 | **0.785** | **0.024** |
| pydantic | 0.519 | **0.555** | **0.003** |

On the four never-touched repos the frozen 4-leg is statistically
indistinguishable from shipped (all p>0.39) — it helps where retrieval
is hard, and does not hurt where it is easy. Product note: the dense leg
requires sentence-transformers (a heavy optional dependency for a
deliberately dependency-free tool); this is measured and available as an
opt-in decision, not silently shipped.

## 7. Cutoff policies — the measured precision lever
*(same runs; policies applied to the shipped-weight ranking, then the
token budget; `gap50` = cut at the largest relative score drop)*

The largest-gap dynamic cutoff is F1-optimal on **all five repos** and
changes the operating point qualitatively — roughly 4× the precision of
top-20 at 6–9 retrieved symbols instead of 20:

| repo | topk_20 P / R | gap50 P / R | avg symbols |
|---|---|---|---|
| click | 0.099 / 0.645 | 0.299 / 0.398 | 6.4 |
| django | 0.100 / 0.721 | 0.380 / 0.519 | 7.5 |
| flask | 0.098 / 0.614 | 0.305 / 0.393 | 8.9 |
| httpx | 0.153 / 0.646 | 0.426 / 0.423 | 6.0 |
| pydantic | 0.091 / 0.495 | 0.347 / 0.319 | 6.8 |

Recall costs ~30% relative — this is not a free lunch and top-k remains
the right default for recall-first use; but for token-priced callers the
gap cutoff is the first measured operating point where precision stops
being embarrassing. (Combined with §2: even GT-adjusted, top-k precision
stays under 0.15, so this lever, not GT noise, is the answer to the
precision problem.)

## Summary of claim changes

1. Cite **r=0.29 (p=0.0001)** for score-recall association — never the
   old r=0.274 (polluted index), never the legacy formula (r=0.02).
2. The score is a **ranking signal**; "confidence" requires the fitted
   per-repo calibration (`--save-calibration`), which is now measured
   and shipped.
3. Hybrid weights: cite **[0.3, 0.5, 0.2]** (LORO-selected) going
   forward; the 0.5/0.35/0.15 numbers stand but were mildly overfit.
4. Dense retrieval: weaker than BM25 as a standalone on these repos
   (with a NL encoder), unique in the failure buckets, and worth
   +3–4 significant recall points as a fourth blend leg on hard repos.
5. Adaptive per-query blending: **null**, do not claim.
6. GT incompleteness: measured, small; precision conclusions stand.
