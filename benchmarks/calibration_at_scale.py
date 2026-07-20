#!/usr/bin/env python3
"""
calibration_at_scale.py — Does the sufficiency score mean anything? Measured
properly this time.

What was wrong before (and what this fixes):
  * The only calibration number on record (Pearson r=0.274) was measured on
    the polluted index (pre-.gitignore-fix) and never re-run.  -> Re-run on
    clean indexes.
  * n≈25 cases per run — r=0.274 at n=25 is p≈0.18, statistically nothing.
    -> Mine up to MAX_CASES per repo across MANY repos, pool and per-repo.
  * Score/100 was never tested AS A PREDICTION of recall (no ECE, no Brier,
    no baseline).  -> Expected calibration error, MAE, Brier vs the trivial
    predict-the-base-rate baseline.
  * Component weights (0.45/0.30/0.15/0.10) and verdict thresholds were
    hand-set and never validated.  -> Fit weights by least squares with
    leave-one-repo-out validation; report whether LEARNED beats HAND-SET
    out of repo. If neither predicts, that is a null result and is printed
    as such.

Everything runs through the REAL product path (index_repository ->
analyze_impact -> compile -> analyze_sufficiency), not a research fork.

Usage:
  python benchmarks/calibration_at_scale.py                      # default repo set
  python benchmarks/calibration_at_scale.py --repos click flask  # subset
  python benchmarks/calibration_at_scale.py --max-cases 150
"""

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diffcontext.pipeline import index_repository
from diffcontext.verify.cases import cases_from_history, run_cases

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "results", "calibration")
DEFAULT_REPOS = ["click", "django", "flask", "httpx", "pydantic",
                 "black", "requests", "rich", "starlette"]
MAX_CASES = 120
COMPONENTS = ("direct_closure", "high_score_retention",
              "local_graph_confidence", "parse_health")
HAND_WEIGHTS = np.array([0.45, 0.30, 0.15, 0.10])
PASS_THRESHOLD = 0.5          # history cases use min_recall=0.5


# ---------------------------------------------------------------------------
# Stats helpers (numpy-only; permutation p-values, no scipy dependency)
# ---------------------------------------------------------------------------

def pearson(x: np.ndarray, y: np.ndarray) -> Optional[float]:
    if x.size < 3 or x.std() == 0 or y.std() == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def spearman(x: np.ndarray, y: np.ndarray) -> Optional[float]:
    def rank(a):
        order = np.argsort(a)
        r = np.empty_like(order, dtype=float)
        r[order] = np.arange(a.size)
        # average ties
        for v in np.unique(a):
            m = a == v
            r[m] = r[m].mean()
        return r
    return pearson(rank(x), rank(y))


def perm_p_corr(x: np.ndarray, y: np.ndarray, corr_fn, n_perm: int = 10000,
                seed: int = 42) -> Optional[float]:
    """Two-sided permutation p-value for a correlation statistic."""
    obs = corr_fn(x, y)
    if obs is None:
        return None
    rng = np.random.default_rng(seed)
    count = 0
    yy = y.copy()
    for _ in range(n_perm):
        rng.shuffle(yy)
        r = corr_fn(x, yy)
        if r is not None and abs(r) >= abs(obs):
            count += 1
    return (count + 1) / (n_perm + 1)


def ece(pred: np.ndarray, actual: np.ndarray, n_bins: int = 5) -> float:
    """Expected calibration error over equal-width prediction bins."""
    edges = np.linspace(0, 1, n_bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (pred >= lo) & ((pred < hi) | (hi == 1.0))
        if m.sum() == 0:
            continue
        total += (m.sum() / pred.size) * abs(pred[m].mean() - actual[m].mean())
    return float(total)


def brier(pred_prob: np.ndarray, event: np.ndarray) -> float:
    return float(np.mean((pred_prob - event) ** 2))


def fit_ls_weights(X: np.ndarray, y: np.ndarray) -> Tuple[np.ndarray, float]:
    """Least-squares fit y ≈ X @ w + b. Returns (w, b)."""
    A = np.hstack([X, np.ones((X.shape[0], 1))])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    return coef[:-1], float(coef[-1])


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def collect_repo(repo_path: str, max_cases: int) -> List[Dict]:
    name = os.path.basename(os.path.abspath(repo_path))
    t0 = time.perf_counter()
    sha = os.popen(f"git -C {repo_path} rev-parse HEAD").read().strip()[:10]
    idx = index_repository(repo_path)
    cases = cases_from_history(repo_path, max_cases=max_cases)
    if not cases:
        print(f"  [{name}] no mineable cases, skipping")
        return []
    results = run_cases(repo_path, cases, index=idx)
    rows = []
    for r in results:
        if r.sufficiency is None:
            continue
        s = r.sufficiency
        rows.append({
            "repo": name,
            "repo_sha": sha,
            "case": r.case.name,
            "n_gt": len(r.case.must_include),
            "recall": r.recall,
            "passed": int(r.passed),
            "score": s.score,
            "score_legacy": s.score_legacy,
            "evidence": s.evidence,
            "direct_closure": s.direct_closure,
            "high_score_retention": s.high_score_retention,
            "local_graph_confidence": s.local_graph_confidence,
            "parse_health": s.parse_health,
            "selected_count": r.selected_count,
            # extended runtime-available features (for the "can ANYTHING
            # structural predict recall?" fit — all computable pre-hoc)
            "n_missing_direct": len(s.missing_direct),
            "n_dropped_high": len(s.dropped_high_score),
            "context_tokens": r.context_tokens,
        })
    print(f"  [{name}] {len(rows)} cases (index {len(idx.symbols)} symbols) "
          f"in {time.perf_counter()-t0:.0f}s", flush=True)
    return rows


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

def correlation_block(score: np.ndarray, recall: np.ndarray) -> Dict:
    pr = pearson(score, recall)
    sr = spearman(score, recall)
    return {
        "n": int(score.size),
        "pearson_r": round(pr, 4) if pr is not None else None,
        "pearson_p": (round(perm_p_corr(score, recall, pearson), 4)
                      if pr is not None else None),
        "spearman_rho": round(sr, 4) if sr is not None else None,
        "spearman_p": (round(perm_p_corr(score, recall, spearman), 4)
                       if sr is not None else None),
        "score_mean": round(float(score.mean()), 2),
        "score_std": round(float(score.std()), 2),
        "recall_mean": round(float(recall.mean()), 4),
    }


def prediction_block(pred: np.ndarray, recall: np.ndarray,
                     passed: np.ndarray, baseline_value: float,
                     baseline_rate: float) -> Dict:
    """Judge `pred` (in [0,1]) as a prediction of recall / of pass."""
    pred = np.clip(pred, 0.0, 1.0)
    return {
        "mae_vs_recall": round(float(np.mean(np.abs(pred - recall))), 4),
        "baseline_mae": round(float(np.mean(np.abs(baseline_value - recall))), 4),
        "ece_5bin": round(ece(pred, recall), 4),
        "brier_pass": round(brier(pred, passed), 4),
        "baseline_brier_pass": round(brier(np.full_like(pred, baseline_rate),
                                           passed), 4),
    }


def reliability_table(score: np.ndarray, recall: np.ndarray) -> List[Dict]:
    out = []
    for lo in (0, 20, 40, 60, 80):
        hi = lo + 20
        m = (score >= lo) & ((score < hi) | (hi == 100))
        out.append({
            "bucket": f"{lo}-{hi}",
            "n": int(m.sum()),
            "mean_score": round(float(score[m].mean()), 1) if m.any() else None,
            "mean_recall": round(float(recall[m].mean()), 4) if m.any() else None,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", nargs="*", default=None)
    ap.add_argument("--max-cases", type=int, default=MAX_CASES)
    args = ap.parse_args()

    bench = os.path.normpath(os.path.join(os.path.dirname(__file__), "..",
                                          "benchmark_repos"))
    os.makedirs(RESULTS_DIR, exist_ok=True)
    repos = args.repos if args.repos else DEFAULT_REPOS

    print(f"Collecting sufficiency/recall pairs from: {repos}", flush=True)
    rows: List[Dict] = []
    for name in repos:
        path = os.path.join(bench, name)
        if not os.path.isdir(path):
            print(f"  !! missing {path}, skipping")
            continue
        rows.extend(collect_repo(path, args.max_cases))

    if not rows:
        print("No data collected.")
        return

    repo_names = sorted({r["repo"] for r in rows})
    score = np.array([r["score"] for r in rows])
    recall = np.array([r["recall"] for r in rows])
    passed = np.array([r["passed"] for r in rows], dtype=float)
    X = np.array([[r[c] for c in COMPONENTS] for r in rows])
    repo_of = np.array([r["repo"] for r in rows])

    report: Dict = {"repos": repo_names, "n_total": len(rows),
                    "components": list(COMPONENTS),
                    "hand_weights": HAND_WEIGHTS.tolist()}

    # ── 1. Correlation: score vs recall, pooled and per repo ───────────────
    print("\n=== 1. Correlation (hand-set score vs measured recall) ===")
    report["pooled_correlation"] = correlation_block(score, recall)
    pc = report["pooled_correlation"]
    print(f"  pooled: n={pc['n']}  pearson r={pc['pearson_r']} (p={pc['pearson_p']})  "
          f"spearman rho={pc['spearman_rho']} (p={pc['spearman_p']})")
    report["per_repo_correlation"] = {}
    for rn in repo_names:
        m = repo_of == rn
        blk = correlation_block(score[m], recall[m])
        report["per_repo_correlation"][rn] = blk
        print(f"  {rn:<10} n={blk['n']:<4} r={blk['pearson_r']} (p={blk['pearson_p']}) "
              f"rho={blk['spearman_rho']} score μ±σ {blk['score_mean']}±{blk['score_std']} "
              f"recall μ {blk['recall_mean']}")

    # ── 1b. Evidence-aware vs legacy score discrimination ──────────────────
    print("\n=== 1b. Evidence-aware score vs legacy score ===")
    legacy = np.array([r["score_legacy"] for r in rows])
    report["legacy_pooled_correlation"] = correlation_block(legacy, recall)
    lc = report["legacy_pooled_correlation"]
    print(f"  legacy : r={lc['pearson_r']} (p={lc['pearson_p']}) "
          f"score μ±σ {lc['score_mean']}±{lc['score_std']}")
    print(f"  new    : r={pc['pearson_r']} (p={pc['pearson_p']}) "
          f"score μ±σ {pc['score_mean']}±{pc['score_std']}")
    report["legacy_per_repo"] = {}
    for rn in repo_names:
        m = repo_of == rn
        report["legacy_per_repo"][rn] = correlation_block(legacy[m], recall[m])

    # ── 2. Score/100 as a PREDICTION of recall ─────────────────────────────
    print("\n=== 2. Hand-set score/100 as a prediction of recall ===")
    base_recall = float(recall.mean())
    base_rate = float(passed.mean())
    report["hand_score_prediction"] = prediction_block(
        score / 100.0, recall, passed, base_recall, base_rate)
    hp = report["hand_score_prediction"]
    print(f"  MAE {hp['mae_vs_recall']} vs baseline(predict mean={base_recall:.3f}) "
          f"{hp['baseline_mae']}   ECE {hp['ece_5bin']}   "
          f"Brier(pass) {hp['brier_pass']} vs baseline {hp['baseline_brier_pass']}")
    report["reliability_hand"] = reliability_table(score, recall)
    for b in report["reliability_hand"]:
        print(f"    score {b['bucket']:<7} n={b['n']:<4} mean_recall={b['mean_recall']}")

    # ── 3. Learned weights, leave-one-repo-out ─────────────────────────────
    print("\n=== 3. Learned component weights (LORO) vs hand-set ===")
    loro = {}
    for held in repo_names:
        tr = repo_of != held
        te = ~tr
        if tr.sum() < 10 or te.sum() < 5:
            continue
        w, b = fit_ls_weights(X[tr], recall[tr])
        pred_te = X[te] @ w + b
        hand_te = (X[te] @ HAND_WEIGHTS)          # hand score in [0,1]
        blk = {
            "n_train": int(tr.sum()), "n_test": int(te.sum()),
            "weights": [round(float(v), 4) for v in w],
            "intercept": round(b, 4),
            "learned": prediction_block(pred_te, recall[te], passed[te],
                                        float(recall[tr].mean()),
                                        float(passed[tr].mean())),
            "hand": prediction_block(hand_te, recall[te], passed[te],
                                     float(recall[tr].mean()),
                                     float(passed[tr].mean())),
            "learned_pearson_heldout": (
                round(pearson(pred_te, recall[te]), 4)
                if pearson(pred_te, recall[te]) is not None else None),
        }
        loro[held] = blk
        print(f"  held={held:<10} learned w={blk['weights']} b={blk['intercept']} "
              f"| MAE learned {blk['learned']['mae_vs_recall']} "
              f"vs hand {blk['hand']['mae_vs_recall']} "
              f"vs baseline {blk['learned']['baseline_mae']} "
              f"| held-out r={blk['learned_pearson_heldout']}")
    report["loro_learned_weights"] = loro

    # ── 3b. Extended features: can ANY runtime-available structural signal
    #        predict recall out-of-repo? ────────────────────────────────────
    print("\n=== 3b. Extended-feature fit (components + counts), LORO ===")
    EXT = list(COMPONENTS) + ["selected_count", "n_missing_direct",
                              "n_dropped_high", "context_tokens"]
    Xe = np.array([[r[c] for c in EXT] for r in rows], dtype=float)
    # standardize count features so lstsq is well-conditioned
    mu, sd = Xe.mean(axis=0), Xe.std(axis=0)
    sd[sd == 0] = 1.0
    Xz = (Xe - mu) / sd
    ext = {}
    for held in repo_names:
        tr = repo_of != held
        te = ~tr
        if tr.sum() < 10 or te.sum() < 5:
            continue
        w, b = fit_ls_weights(Xz[tr], recall[tr])
        pred_te = Xz[te] @ w + b
        r_held = pearson(pred_te, recall[te])
        ext[held] = {
            "mae": round(float(np.mean(np.abs(np.clip(pred_te, 0, 1) - recall[te]))), 4),
            "baseline_mae": round(float(np.mean(np.abs(recall[tr].mean() - recall[te]))), 4),
            "heldout_pearson": round(r_held, 4) if r_held is not None else None,
        }
        print(f"  held={held:<10} MAE {ext[held]['mae']} vs baseline "
              f"{ext[held]['baseline_mae']} | held-out r={ext[held]['heldout_pearson']}")
    report["loro_extended_features"] = {"features": EXT, "folds": ext}

    # ── 4. Verdict ─────────────────────────────────────────────────────────
    print("\n=== 4. Verdict ===")
    pooled_p = report["pooled_correlation"]["pearson_p"]
    verdicts = []
    if pooled_p is None or pooled_p > 0.05:
        verdicts.append(
            "NULL RESULT: the structural score does not significantly track "
            "recall on pooled data. It must not be presented as calibrated "
            "confidence.")
    else:
        verdicts.append(
            f"The score-recall association is statistically detectable pooled "
            f"(r={report['pooled_correlation']['pearson_r']}, p={pooled_p}).")
    mae_h = report["hand_score_prediction"]["mae_vs_recall"]
    mae_b = report["hand_score_prediction"]["baseline_mae"]
    if mae_h >= mae_b:
        verdicts.append(
            "As a PREDICTOR of recall, score/100 does NOT beat predicting the "
            "mean — the score is (at best) a ranking signal, not a probability.")
    else:
        verdicts.append("score/100 beats the trivial mean predictor on MAE.")
    if loro:
        wins = sum(1 for v in loro.values()
                   if v["learned"]["mae_vs_recall"] < v["hand"]["mae_vs_recall"])
        verdicts.append(f"Learned weights beat hand-set weights on held-out MAE in "
                        f"{wins}/{len(loro)} repos.")
    for v in verdicts:
        print(f"  * {v}")
    report["verdicts"] = verdicts

    out = os.path.join(RESULTS_DIR, "calibration_at_scale.json")
    with open(out, "w") as f:
        json.dump({"report": report, "rows": rows}, f, indent=2)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
