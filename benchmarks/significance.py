#!/usr/bin/env python3
"""
significance.py — paired significance testing over eval_v2 case rows.

Bootstrap CIs (already in eval_v2) quantify one method's uncertainty;
they do not establish that method A beats method B on the SAME cases.
This module adds the standard paired test for that claim: the Wilcoxon
signed-rank test between two methods' per-commit metric values, computed
over identical commits (a commit counts once, matching eval_v2's primary
aggregate). "Hybrid beats BM25" becomes a stated p-value instead of an
eyeballed CI overlap — the exact gap a reviewer flags first
(docs/PLAN.md §3.5).

Implementation notes:
  * Pure stdlib. The normal approximation with tie correction and 0.5
    continuity correction is used (standard for n > ~25; eval_v2 has
    ~50-100 commits per repo). Zero-difference pairs are dropped
    (Wilcoxon's original treatment).
  * Two-sided p-values. With the small number of planned comparisons we
    report raw p-values plus Holm-Bonferroni adjusted ones.

Usage:
  python benchmarks/significance.py                       # all repos with saved cases
  python benchmarks/significance.py flask                 # one repo
  python benchmarks/significance.py flask --metric recall
"""

import csv
import math
import os
import sys
from collections import defaultdict
from typing import Dict, List, Sequence, Tuple

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "results", "eval_v2")

PRIMARY = "hybrid_full"        # the product's newest blend
COMPARISONS = ["hybrid", "diffcontext", "bm25", "embedding", "samefile", "random_k"]
DEFAULT_METRICS = ["recall", "hit", "f1"]


def wilcoxon_signed_rank(x: Sequence[float], y: Sequence[float]) -> Tuple[float, float, int]:
    """
    Two-sided Wilcoxon signed-rank test for paired samples.

    Returns (W, p_value, n_effective) where W is the smaller of the
    signed-rank sums and n_effective the number of non-zero differences.
    Normal approximation with tie correction and continuity correction;
    returns p=1.0 when fewer than 6 non-zero differences exist (the test
    has no power there, and the exact tables are pointless for our n).
    """
    diffs = [a - b for a, b in zip(x, y) if a != b]
    n = len(diffs)
    if n < 6:
        return 0.0, 1.0, n

    # rank |d| with average ranks for ties
    order = sorted(range(n), key=lambda i: abs(diffs[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(diffs[order[j + 1]]) == abs(diffs[order[i]]):
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1

    w_plus = sum(r for r, d in zip(ranks, diffs) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, diffs) if d < 0)
    w = min(w_plus, w_minus)

    mean_w = n * (n + 1) / 4.0
    var_w = n * (n + 1) * (2 * n + 1) / 24.0
    # tie correction: subtract sum(t^3 - t)/48 over tie groups of |d|
    tie_groups: Dict[float, int] = defaultdict(int)
    for d in diffs:
        tie_groups[abs(d)] += 1
    var_w -= sum(t ** 3 - t for t in tie_groups.values()) / 48.0
    if var_w <= 0:
        return w, 1.0, n

    z = (w - mean_w + 0.5) / math.sqrt(var_w)   # continuity-corrected
    p = 2.0 * _norm_sf(abs(z))
    return w, min(1.0, p), n


def _norm_sf(z: float) -> float:
    """Standard normal survival function via erfc."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def holm_bonferroni(pvals: List[float]) -> List[float]:
    """Holm-Bonferroni adjusted p-values (monotone, capped at 1)."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adjusted = [0.0] * m
    running_max = 0.0
    for rank, idx in enumerate(order):
        adj = min(1.0, (m - rank) * pvals[idx])
        running_max = max(running_max, adj)
        adjusted[idx] = running_max
    return adjusted


def per_commit_values(csv_path: str, metric: str) -> Dict[str, Dict[str, float]]:
    """method -> {commit: mean metric over that commit's query rows}."""
    acc: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            acc[row["method"]][row["commit"]].append(float(row[metric]))
    return {
        method: {c: sum(v) / len(v) for c, v in commits.items()}
        for method, commits in acc.items()
    }


def compare_repo(csv_path: str, metrics: List[str]) -> List[Dict]:
    repo = os.path.basename(csv_path).replace("_cases.csv", "")
    out: List[Dict] = []
    for metric in metrics:
        values = per_commit_values(csv_path, metric)
        if PRIMARY not in values:
            print(f"  [{repo}] no '{PRIMARY}' rows — re-run eval_v2 first")
            return out
        rows = []
        for other in COMPARISONS:
            if other not in values:
                continue
            commits = sorted(set(values[PRIMARY]) & set(values[other]))
            x = [values[PRIMARY][c] for c in commits]
            y = [values[other][c] for c in commits]
            w, p, n_eff = wilcoxon_signed_rank(x, y)
            rows.append({
                "repo": repo, "metric": metric,
                "a": PRIMARY, "b": other,
                "mean_a": sum(x) / len(x) if x else 0.0,
                "mean_b": sum(y) / len(y) if y else 0.0,
                "n_commits": len(commits), "n_nonzero_diffs": n_eff,
                "W": w, "p": p,
            })
        for row, p_adj in zip(rows, holm_bonferroni([r["p"] for r in rows])):
            row["p_holm"] = p_adj
        out.extend(rows)
    return out


def main():
    args = sys.argv[1:]
    metrics = DEFAULT_METRICS
    if "--metric" in args:
        i = args.index("--metric")
        metrics = [args[i + 1]]
        args = args[:i] + args[i + 2:]

    if args:
        paths = [os.path.join(RESULTS_DIR, f"{a}_cases.csv") for a in args]
    else:
        paths = sorted(
            os.path.join(RESULTS_DIR, f) for f in os.listdir(RESULTS_DIR)
            if f.endswith("_cases.csv")
        ) if os.path.isdir(RESULTS_DIR) else []
    if not paths:
        print("No eval_v2 case CSVs found — run benchmarks/eval_v2_hardened.py first.")
        sys.exit(1)

    all_rows: List[Dict] = []
    for p in paths:
        if not os.path.exists(p):
            print(f"missing: {p}")
            continue
        all_rows.extend(compare_repo(p, metrics))

    if not all_rows:
        sys.exit(1)

    hdr = (f"{'repo':<10}{'metric':<8}{'A (primary)':<15}{'B':<13}"
           f"{'mean A':>8}{'mean B':>8}{'n':>5}{'p':>10}{'p(Holm)':>10}  verdict")
    print("\n" + hdr)
    print("-" * len(hdr))
    for r in all_rows:
        verdict = ("A > B *" if r["p_holm"] < 0.05 and r["mean_a"] > r["mean_b"]
                   else "B > A *" if r["p_holm"] < 0.05 else "n.s.")
        print(f"{r['repo']:<10}{r['metric']:<8}{r['a']:<15}{r['b']:<13}"
              f"{r['mean_a']:>8.3f}{r['mean_b']:>8.3f}{r['n_commits']:>5}"
              f"{r['p']:>10.4f}{r['p_holm']:>10.4f}  {verdict}")
    print("\n* Holm-adjusted p < 0.05 (two-sided Wilcoxon signed-rank, per-commit pairs)")


if __name__ == "__main__":
    main()
