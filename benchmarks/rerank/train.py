"""
train.py — fit the stage-2 reranker. Benchmark-side; never imported by the package.

L2-regularized logistic regression on the mined pools, optimized with
`scipy.optimize.minimize(method="L-BFGS-B")` on the exact logistic loss with an
analytic gradient. numpy and scipy are fine here because this runs offline; the
artifact it emits (`diffcontext/rerank/weights.json`) is consumed by pure-stdlib
inference in `diffcontext/rerank/model.py`.

Validation protocol, in the order the brief mandates:

  loro      train on 4 of {django,click,flask,httpx,pydantic}, test on the 5th.
            Regularization strength is chosen by an INNER leave-one-repo-out
            sweep over the training repos only — the held-out repo is never
            consulted for any decision.
  temporal  within each repo, train on older commits, test on newer. Co-change
            ground truth leaks across time otherwise.
  frozen    train on all five, test on repos never used for any tuning
            decision (black, requests, rich, starlette).

    python -m benchmarks.rerank.train --mode loro
    python -m benchmarks.rerank.train --mode frozen --emit
"""

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import minimize

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diffcontext.rerank.features import FEATURE_NAMES, N_FEATURES
from benchmarks.significance import wilcoxon_signed_rank, holm_bonferroni

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(REPO_ROOT, "benchmarks", "results", "rerank")
WEIGHTS_OUT = os.path.join(REPO_ROOT, "diffcontext", "rerank", "weights.json")

TRAIN_REPOS = ["django", "click", "flask", "httpx", "pydantic"]
FROZEN_REPOS = ["black", "requests", "rich", "starlette"]

LAMBDAS = [3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1]

# v1 ships WITHOUT the co-change feature. It is only populated when the user
# passes --with-history, and a model that leaned on it would silently degrade
# for everyone who does not. The column is mined and cached so a history-aware
# v2 needs no re-mining; here it is zeroed and its coefficient pinned to 0.
COCHANGE_IDX = list(FEATURE_NAMES).index("cochange_assoc")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

class RepoData:
    def __init__(self, repo: str):
        z = np.load(os.path.join(DATA_DIR, f"{repo}.npz"))
        with open(os.path.join(DATA_DIR, f"{repo}.meta.json")) as fh:
            meta = json.load(fh)
        self.repo = repo
        self.X = z["X"].astype(np.float64)
        self.y = z["y"].astype(np.float64)
        self.qid = z["qid"].astype(np.int64)
        self.meta = meta
        self.groups = {g["qid"]: g for g in meta["groups"]}
        if meta["feature_names"] != list(FEATURE_NAMES):
            raise SystemExit(
                f"{repo}: cached features are stale (feature contract changed). "
                "Re-run benchmarks.rerank.mine."
            )
        # Row slices per query, so ranking metrics never re-scan the array.
        self.slices: Dict[int, slice] = {}
        starts = np.searchsorted(self.qid, np.unique(self.qid), side="left")
        ends = np.searchsorted(self.qid, np.unique(self.qid), side="right")
        for q, a, b in zip(np.unique(self.qid), starts, ends):
            self.slices[int(q)] = slice(int(a), int(b))


def load(repos: Sequence[str]) -> Dict[str, RepoData]:
    out = {}
    for r in repos:
        p = os.path.join(DATA_DIR, f"{r}.npz")
        if not os.path.exists(p):
            print(f"  !! missing mined data for {r} ({p}) — skipping")
            continue
        out[r] = RepoData(r)
    return out


def stack(datas: Sequence[RepoData]) -> Tuple[np.ndarray, np.ndarray]:
    return (np.vstack([d.X for d in datas]),
            np.concatenate([d.y for d in datas]))


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def standardizer(X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Mean/scale from TRAINING rows only. Constant columns get scale 1.0 so
    inference never divides by zero."""
    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale[scale < 1e-12] = 1.0
    return mean, scale


def fit(
    X: np.ndarray, y: np.ndarray, lam: float, pinned: Optional[Sequence[int]] = None,
) -> Tuple[np.ndarray, float]:
    """Class-weighted L2 logistic regression. Returns (coef, intercept).

    `pinned` columns are held at coefficient 0 by zeroing their gradient.
    """
    n, d = X.shape
    n_pos = float(y.sum())
    n_neg = float(n - n_pos)
    if n_pos == 0 or n_neg == 0:
        raise SystemExit("degenerate training set: one class is empty")
    # Balanced weights: positives are ~4% of rows, so unweighted training
    # would find "predict 0 everywhere" is 96% accurate and stop there.
    w_pos, w_neg = n / (2.0 * n_pos), n / (2.0 * n_neg)
    s = np.where(y > 0, w_pos, w_neg)
    W = s.sum()
    pin = np.zeros(d, dtype=bool)
    if pinned:
        pin[list(pinned)] = True

    def obj(theta):
        w, b = theta[:d], theta[d]
        z = X @ w + b
        # log(1+exp(z)) computed stably
        ll = np.maximum(z, 0.0) + np.log1p(np.exp(-np.abs(z)))
        loss = float((s * (ll - y * z)).sum() / W + 0.5 * lam * (w @ w))
        p = np.empty_like(z)
        pos = z >= 0
        p[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
        ez = np.exp(z[~pos])
        p[~pos] = ez / (1.0 + ez)
        r = s * (p - y)
        gw = X.T @ r / W + lam * w
        gw[pin] = 0.0
        gb = float(r.sum() / W)
        return loss, np.concatenate([gw, [gb]])

    theta0 = np.zeros(d + 1)
    res = minimize(obj, theta0, jac=True, method="L-BFGS-B",
                   options={"maxiter": 500, "ftol": 1e-10})
    return res.x[:d], float(res.x[d])


def fit_pairwise(
    groups: List[Tuple[np.ndarray, np.ndarray]], d: int, lam: float,
    pinned: Optional[Sequence[int]] = None,
) -> Tuple[np.ndarray, float]:
    """RankNet-style pairwise logistic ranking, linear scorer.

    Pointwise logistic asks "is this candidate a co-change?" and is dominated
    by the 96% of rows that are negatives. The metric we actually care about
    is *within-query ordering*, so optimize that directly: for every
    (positive, negative) pair inside a query, push the positive above the
    negative.

    The intercept cancels in every pairwise difference, so it is not
    identifiable here and is returned as 0.0. It does not affect ranking;
    calibration is a separate, post-hoc step (see calibrate.py).

    `groups` is a list of (X_pos, X_neg) already standardized.
    """
    pin = np.zeros(d, dtype=bool)
    if pinned:
        pin[list(pinned)] = True
    n_pairs = sum(p.shape[0] * n.shape[0] for p, n in groups) or 1

    def obj(w):
        loss = 0.0
        grad = np.zeros(d)
        for Xp, Xn in groups:
            zp = Xp @ w                      # (P,)
            zn = Xn @ w                      # (N,)
            diff = zp[:, None] - zn[None, :]  # (P,N) margin, want > 0
            loss += float((np.maximum(-diff, 0.0)
                           + np.log1p(np.exp(-np.abs(diff)))).sum())
            s = 1.0 / (1.0 + np.exp(diff))    # sigma(-diff) = dL/d(diff)
            grad -= Xp.T @ s.sum(axis=1)
            grad += Xn.T @ s.sum(axis=0)
        loss = loss / n_pairs + 0.5 * lam * (w @ w)
        grad = grad / n_pairs + lam * w
        grad[pin] = 0.0
        return loss, grad

    res = minimize(obj, np.zeros(d), jac=True, method="L-BFGS-B",
                   options={"maxiter": 300, "ftol": 1e-9})
    return res.x, 0.0


def platt(z: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
    """Fit p = sigmoid(a*z + b) on out-of-fold scores. Returns (a, b).

    The pairwise objective is scale- and shift-free: only differences matter,
    so its raw score is an ordering, not a probability. Platt scaling recovers
    a probability WITHOUT touching the ordering (a > 0 is monotone), which is
    what makes `cutoff="prob"` meaningful. Folding (a, b) back into the
    coefficients keeps weights.json a plain linear model.
    """
    def obj(t):
        a, b = t
        zz = a * z + b
        # log(1+exp(zz)) and sigmoid(zz), both overflow-safe
        ll = np.maximum(zz, 0.0) + np.log1p(np.exp(-np.abs(zz)))
        loss = float((ll - y * zz).mean())
        e = np.exp(-np.abs(zz))
        p = np.where(zz >= 0, 1.0 / (1.0 + e), e / (1.0 + e))
        r = p - y
        return loss, np.array([float((r * z).mean()), float(r.mean())])

    res = minimize(obj, np.array([1.0, -3.0]), jac=True, method="L-BFGS-B",
                   options={"maxiter": 200})
    a, b = float(res.x[0]), float(res.x[1])
    if a <= 0:      # would invert the ranking; refuse rather than corrupt it
        return 1.0, 0.0
    return a, b


def expected_calibration_error(
    p: np.ndarray, y: np.ndarray, bins: int = 10
) -> Tuple[float, List[Dict]]:
    """10-bin reliability curve + ECE."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    ece, curve, n = 0.0, [], len(p)
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        m = (p >= lo) & (p < hi if i < bins - 1 else p <= hi)
        if not m.any():
            curve.append({"bin": f"[{lo:.1f},{hi:.1f})", "n": 0,
                          "predicted": None, "empirical": None})
            continue
        pred, emp = float(p[m].mean()), float(y[m].mean())
        ece += (m.sum() / n) * abs(pred - emp)
        curve.append({"bin": f"[{lo:.1f},{hi:.1f})", "n": int(m.sum()),
                      "predicted": pred, "empirical": emp})
    return float(ece), curve


def make_pairs(
    datas: Sequence[RepoData], mean: np.ndarray, scale: np.ndarray,
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Per-query (standardized positives, standardized negatives)."""
    out = []
    for d in datas:
        for q, sl in d.slices.items():
            Xq = (d.X[sl] - mean) / scale
            yq = d.y[sl]
            pos, neg = Xq[yq > 0], Xq[yq == 0]
            if pos.shape[0] and neg.shape[0]:
                out.append((pos, neg))
    return out


# ---------------------------------------------------------------------------
# Ranking metrics
# ---------------------------------------------------------------------------

KS = (5, 10, 20)


def per_query_metrics(
    data: RepoData, mean: np.ndarray, scale: np.ndarray,
    coef: np.ndarray, intercept: float,
) -> Tuple[List[Dict], List[Dict]]:
    """(reranked, baseline) per-query metric dicts, on identical case sets.

    The baseline is the stage-1 pool in its own order — that IS the shipped
    ranking, so this is a paired comparison on the same queries, not a
    re-derivation of published numbers.
    """
    rr, bl = [], []
    for q, sl in data.slices.items():
        Xq = (data.X[sl] - mean) / scale
        yq = data.y[sl]
        z = Xq @ coef + intercept
        order = np.argsort(-z, kind="stable")     # stable => ties keep stage-1 order
        n_gt = data.groups[q]["n_gt"]
        m_rr, m_bl = {}, {}
        for k in KS:
            hits_rr = float(yq[order[:k]].sum())
            hits_bl = float(yq[:k].sum())
            m_rr[f"p@{k}"] = hits_rr / k
            m_bl[f"p@{k}"] = hits_bl / k
            m_rr[f"r@{k}"] = hits_rr / n_gt if n_gt else 0.0
            m_bl[f"r@{k}"] = hits_bl / n_gt if n_gt else 0.0
        for m in (m_rr, m_bl):
            p, r = m["p@10"], m["r@10"]
            m["f1@10"] = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        rr.append(m_rr)
        bl.append(m_bl)
    return rr, bl


def mean_of(rows: List[Dict], key: str) -> float:
    return float(np.mean([r[key] for r in rows])) if rows else 0.0


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

def train_one(
    train: Sequence[RepoData], lam: float, pinned, objective: str,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Fit on `train`. Returns (mean, scale, coef, intercept)."""
    Xt, yt = stack(train)
    mean, scale = standardizer(Xt)
    if objective == "pairwise":
        coef, b = fit_pairwise(
            make_pairs(train, mean, scale), Xt.shape[1], lam, pinned
        )
    else:
        coef, b = fit((Xt - mean) / scale, yt, lam, pinned)
    return mean, scale, coef, b


def select_lambda(train: List[RepoData], pinned, objective: str) -> float:
    """Inner leave-one-repo-out over the TRAINING repos only."""
    if len(train) < 2:
        return 1e-2
    best, best_score = LAMBDAS[0], -1.0
    for lam in LAMBDAS:
        scores = []
        for held in train:
            inner = [d for d in train if d.repo != held.repo]
            mean, scale, coef, b = train_one(inner, lam, pinned, objective)
            rr, _ = per_query_metrics(held, mean, scale, coef, b)
            scores.append(mean_of(rr, "p@10"))
        s = float(np.mean(scores))
        if s > best_score:
            best, best_score = lam, s
    return best


def run_loro(datas: Dict[str, RepoData], pinned, objective: str = "pointwise") -> List[Dict]:
    rows = []
    for held_name, held in datas.items():
        train = [d for n, d in datas.items() if n != held_name]
        lam = select_lambda(train, pinned, objective)
        mean, scale, coef, b = train_one(train, lam, pinned, objective)
        rr, bl = per_query_metrics(held, mean, scale, coef, b)
        rows.append({
            "fold": held_name, "lam": lam, "n_queries": len(rr),
            "reachable_frac": held.meta["reachable_frac"],
            "rerank": {k: mean_of(rr, k) for k in rr[0]},
            "baseline": {k: mean_of(bl, k) for k in bl[0]},
            "_rr": rr, "_bl": bl,
        })
    return rows


def run_temporal(datas: Dict[str, RepoData], pinned, objective: str = "pointwise",
                 frac: float = 0.7) -> List[Dict]:
    """Train on older commits, test on newer, within each repo."""
    rows = []
    for name, d in datas.items():
        qs = sorted(d.slices, key=lambda q: (d.groups[q]["commit_ts"], q))
        cut = int(len(qs) * frac)
        old, new = qs[:cut], qs[cut:]
        if not old or not new:
            continue
        idx_old = np.concatenate([np.arange(d.slices[q].start, d.slices[q].stop) for q in old])
        Xo, yo = d.X[idx_old], d.y[idx_old]
        mean, scale = standardizer(Xo)
        if objective == "pairwise":
            pairs = []
            for q in old:
                sl = d.slices[q]
                Xq = (d.X[sl] - mean) / scale
                yq = d.y[sl]
                p, ng = Xq[yq > 0], Xq[yq == 0]
                if p.shape[0] and ng.shape[0]:
                    pairs.append((p, ng))
            coef, b = fit_pairwise(pairs, Xo.shape[1], 1e-2, pinned)
        else:
            coef, b = fit((Xo - mean) / scale, yo, 1e-2, pinned)

        rr, bl = [], []
        for q in new:
            sl = d.slices[q]
            Xq = (d.X[sl] - mean) / scale
            yq = d.y[sl]
            order = np.argsort(-(Xq @ coef + b), kind="stable")
            n_gt = d.groups[q]["n_gt"]
            m_rr, m_bl = {}, {}
            for k in KS:
                m_rr[f"p@{k}"] = float(yq[order[:k]].sum()) / k
                m_bl[f"p@{k}"] = float(yq[:k].sum()) / k
                m_rr[f"r@{k}"] = float(yq[order[:k]].sum()) / n_gt if n_gt else 0.0
                m_bl[f"r@{k}"] = float(yq[:k].sum()) / n_gt if n_gt else 0.0
            for m in (m_rr, m_bl):
                p, r = m["p@10"], m["r@10"]
                m["f1@10"] = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
            rr.append(m_rr); bl.append(m_bl)
        rows.append({
            "fold": name, "n_train_q": len(old), "n_test_q": len(new),
            "rerank": {k: mean_of(rr, k) for k in rr[0]},
            "baseline": {k: mean_of(bl, k) for k in bl[0]},
            "_rr": rr, "_bl": bl,
        })
    return rows


def run_temporal_loro(datas: Dict[str, RepoData], pinned,
                      objective: str = "pairwise", frac: float = 0.7) -> List[Dict]:
    """Both guards at once: cross-repo AND forward-in-time.

    Train on the other four repos in full PLUS the held-out repo's OLDER
    commits; test on its NEWER commits. This exists to disambiguate the plain
    temporal result: that split trains on ~200 queries from one repo, so a
    null there could mean "no temporal transfer" or merely "not enough data".
    Here the training set is the same size as LORO's, and only the test set is
    forward-in-time — so a null here cannot be blamed on data volume.
    """
    rows = []
    for name, held in datas.items():
        qs = sorted(held.slices, key=lambda q: (held.groups[q]["commit_ts"], q))
        cut = int(len(qs) * frac)
        old, new = qs[:cut], qs[cut:]
        if not old or not new:
            continue
        others = [d for n, d in datas.items() if n != name]
        idx_old = np.concatenate(
            [np.arange(held.slices[q].start, held.slices[q].stop) for q in old]
        )
        Xt = np.vstack([d.X for d in others] + [held.X[idx_old]])
        yt = np.concatenate([d.y for d in others] + [held.y[idx_old]])
        mean, scale = standardizer(Xt)

        if objective == "pairwise":
            pairs = make_pairs(others, mean, scale)
            for q in old:
                sl = held.slices[q]
                Xq = (held.X[sl] - mean) / scale
                yq = held.y[sl]
                p, ng = Xq[yq > 0], Xq[yq == 0]
                if p.shape[0] and ng.shape[0]:
                    pairs.append((p, ng))
            coef, b = fit_pairwise(pairs, Xt.shape[1], 1e-2, pinned)
        else:
            coef, b = fit((Xt - mean) / scale, yt, 1e-2, pinned)

        rr, bl = [], []
        for q in new:
            sl = held.slices[q]
            Xq = (held.X[sl] - mean) / scale
            yq = held.y[sl]
            order = np.argsort(-(Xq @ coef + b), kind="stable")
            n_gt = held.groups[q]["n_gt"]
            m_rr, m_bl = {}, {}
            for k in KS:
                m_rr[f"p@{k}"] = float(yq[order[:k]].sum()) / k
                m_bl[f"p@{k}"] = float(yq[:k].sum()) / k
                m_rr[f"r@{k}"] = float(yq[order[:k]].sum()) / n_gt if n_gt else 0.0
                m_bl[f"r@{k}"] = float(yq[:k].sum()) / n_gt if n_gt else 0.0
            for m in (m_rr, m_bl):
                p, r = m["p@10"], m["r@10"]
                m["f1@10"] = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
            rr.append(m_rr); bl.append(m_bl)
        rows.append({
            "fold": name, "n_test_q": len(new),
            "n_train_q": sum(len(d.slices) for d in others) + len(old),
            "rerank": {k: mean_of(rr, k) for k in rr[0]},
            "baseline": {k: mean_of(bl, k) for k in bl[0]},
            "_rr": rr, "_bl": bl,
        })
    return rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

REPORT_KEYS = ["p@5", "p@10", "r@10", "r@20", "f1@10"]


def report(rows: List[Dict], title: str) -> Dict:
    print(f"\n{'='*78}\n  {title}\n{'='*78}")
    hdr = f"  {'fold':10s} {'n':>5s} " + " ".join(f"{k:>16s}" for k in REPORT_KEYS)
    print(hdr)
    print(f"  {'':10s} {'':>5s} " + " ".join(f"{'rerank / base':>16s}" for _ in REPORT_KEYS))
    for r in rows:
        n = r.get("n_queries", r.get("n_test_q", 0))
        cells = " ".join(
            f"{r['rerank'][k]:7.3f} /{r['baseline'][k]:7.3f}" for k in REPORT_KEYS
        )
        print(f"  {r['fold']:10s} {n:5d} {cells}")

    pooled_rr = [m for r in rows for m in r["_rr"]]
    pooled_bl = [m for r in rows for m in r["_bl"]]
    cells = " ".join(
        f"{mean_of(pooled_rr,k):7.3f} /{mean_of(pooled_bl,k):7.3f}" for k in REPORT_KEYS
    )
    print(f"  {'POOLED':10s} {len(pooled_rr):5d} {cells}")

    # Paired Wilcoxon per metric, Holm-corrected across the metric family.
    pvals, stats = [], []
    for k in REPORT_KEYS:
        x = [m[k] for m in pooled_rr]
        y = [m[k] for m in pooled_bl]
        stat, p, n_nonzero = wilcoxon_signed_rank(x, y)
        pvals.append(p)
        stats.append((k, stat, p, n_nonzero))
    adj = holm_bonferroni(pvals)
    print(f"\n  paired Wilcoxon (rerank vs shipped stage-1), Holm-corrected:")
    print(f"  {'metric':8s} {'delta':>8s} {'n_eff':>6s} {'p_raw':>10s} {'p_holm':>10s}")
    for (k, stat, p, n_nz), pa in zip(stats, adj):
        d = mean_of(pooled_rr, k) - mean_of(pooled_bl, k)
        flag = "*" if pa < 0.05 else " "
        print(f"  {k:8s} {d:+8.3f} {n_nz:6d} {p:10.4g} {pa:10.4g} {flag}")
    if all(s[3] == 0 for s in stats):
        print("  !! n_eff = 0 on every metric — no query separates the arms.")

    return {
        "pooled": {k: {"rerank": mean_of(pooled_rr, k),
                       "baseline": mean_of(pooled_bl, k)} for k in REPORT_KEYS},
        "n_queries": len(pooled_rr),
        "wilcoxon": [{"metric": k, "p_raw": p, "p_holm": pa, "n_eff": n}
                     for (k, _, p, n), pa in zip(stats, adj)],
        "folds": [{kk: vv for kk, vv in r.items() if not kk.startswith("_")}
                  for r in rows],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["loro", "temporal", "temporal_loro", "frozen", "all"],
                    default="loro")
    ap.add_argument("--use-cochange", action="store_true",
                    help="include feature 22 (requires --with-history at inference)")
    ap.add_argument("--objective", choices=["pointwise", "pairwise"],
                    default="pointwise",
                    help="pointwise logistic (calibrated) or pairwise RankNet "
                         "(optimizes within-query order directly)")
    ap.add_argument("--emit", action="store_true",
                    help="write diffcontext/rerank/weights.json from all TRAIN_REPOS")
    args = ap.parse_args()

    pinned = None if args.use_cochange else [COCHANGE_IDX]
    if pinned:
        print(f"  [feature '{FEATURE_NAMES[COCHANGE_IDX]}' pinned to coef 0 "
              f"— v1 ships without history]")

    datas = load(TRAIN_REPOS)
    if not datas:
        raise SystemExit("no mined data; run `python -m benchmarks.rerank.mine` first")
    for d in datas.values():
        if pinned:
            d.X[:, COCHANGE_IDX] = 0.0
    print(f"  loaded: " + ", ".join(
        f"{n}({d.X.shape[0]}r/{len(d.slices)}q)" for n, d in datas.items()))

    out = {}
    if args.mode in ("loro", "all"):
        out["loro"] = report(run_loro(datas, pinned, args.objective),
                             f"LEAVE-ONE-REPO-OUT [{args.objective}]")
    if args.mode in ("temporal", "all"):
        out["temporal"] = report(run_temporal(datas, pinned, args.objective),
                                 f"TEMPORAL SPLIT (older -> newer) [{args.objective}]")
    if args.mode in ("temporal_loro", "all"):
        out["temporal_loro"] = report(
            run_temporal_loro(datas, pinned, args.objective),
            f"TEMPORAL + CROSS-REPO (4 repos + older -> newer) [{args.objective}]")

    if args.mode in ("frozen", "all"):
        frozen = load(FROZEN_REPOS)
        for d in frozen.values():
            if pinned:
                d.X[:, COCHANGE_IDX] = 0.0
        if frozen:
            train = list(datas.values())
            lam = select_lambda(train, pinned, args.objective)
            mean, scale, coef, b = train_one(train, lam, pinned, args.objective)
            rows = []
            for name, d in frozen.items():
                rr, bl = per_query_metrics(d, mean, scale, coef, b)
                rows.append({"fold": name, "lam": lam, "n_queries": len(rr),
                             "rerank": {k: mean_of(rr, k) for k in rr[0]},
                             "baseline": {k: mean_of(bl, k) for k in bl[0]},
                             "_rr": rr, "_bl": bl})
            out["frozen"] = report(
                rows, f"FROZEN-MODEL VALIDATION (never tuned on) [{args.objective}]")

    if args.emit:
        train = list(datas.values())
        lam = select_lambda(train, pinned, args.objective)
        mean, scale, coef, b = train_one(train, lam, pinned, args.objective)
        Xt, _ = stack(train)

        # Calibrate on OUT-OF-FOLD scores. Calibrating on the training fit
        # would report the model's own optimism back to itself.
        oof_z, oof_y = [], []
        for held_name, held in datas.items():
            inner = [d for n, d in datas.items() if n != held_name]
            m_i, s_i, c_i, b_i = train_one(inner, lam, pinned, args.objective)
            oof_z.append(((held.X - m_i) / s_i) @ c_i + b_i)
            oof_y.append(held.y)
        oof_z = np.concatenate(oof_z)
        oof_y = np.concatenate(oof_y)
        a_cal, b_cal = platt(oof_z, oof_y)
        # Fold the calibration into the linear model: sigmoid(a*(w.x+b0)+b1)
        # is just another linear model, so weights.json stays a plain one.
        coef = a_cal * coef
        b = a_cal * b + b_cal
        p_oof = 1.0 / (1.0 + np.exp(-(a_cal * oof_z + b_cal)))
        ece, curve = expected_calibration_error(p_oof, oof_y)
        print(f"\n  calibration: Platt a={a_cal:.4f} b={b_cal:.4f}  ECE={ece:.4f}")
        print(f"  {'bin':14s} {'n':>8s} {'predicted':>10s} {'empirical':>10s}")
        for row in curve:
            if row["n"]:
                print(f"  {row['bin']:14s} {row['n']:8d} "
                      f"{row['predicted']:10.4f} {row['empirical']:10.4f}")

        blob = {
            "version": 1,
            "feature_names": list(FEATURE_NAMES),
            "mean": [float(v) for v in mean],
            "scale": [float(v) for v in scale],
            "coef": [float(v) for v in coef],
            "intercept": float(b),
            "trained_on": sorted(datas),
            "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_rows": int(Xt.shape[0]),
            "lambda": lam,
            "use_cochange": bool(args.use_cochange),
            "objective": args.objective,
            "calibration": {"platt_a": a_cal, "platt_b": b_cal, "ece": ece,
                            "reliability": curve},
            "metrics": {k: v["pooled"] for k, v in out.items()},
        }
        with open(WEIGHTS_OUT, "w") as fh:
            json.dump(blob, fh, indent=2)
        print(f"\n  wrote {WEIGHTS_OUT} (lambda={lam}, {Xt.shape[0]} rows)")
        print(f"  top coefficients:")
        for i in np.argsort(-np.abs(coef))[:10]:
            print(f"    {FEATURE_NAMES[i]:22s} {coef[i]:+.3f}")

    with open(os.path.join(DATA_DIR, "validation.json"), "w") as fh:
        json.dump(out, fh, indent=2)


if __name__ == "__main__":
    main()
