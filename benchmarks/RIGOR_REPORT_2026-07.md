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
*(script: `blend_loro.py` — TO BE FILLED FROM `results/loro/loro_3leg.json`)*

## 4. Adaptive (per-query dynamic) blend
*(TO BE FILLED FROM `results/loro/loro_3leg.json` / `loro_dense.json`)*

## 5. True dense-embedding baseline
*(TO BE FILLED FROM `results/eval_v2/` re-run with
sentence-transformers/all-MiniLM-L6-v2 on the pinned snapshots)*

## 6. Cutoff policies (precision operating points)
*(TO BE FILLED FROM `results/loro/*.json` `cutoff_policies`)*
