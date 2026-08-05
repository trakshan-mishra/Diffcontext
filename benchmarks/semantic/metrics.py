"""
metrics.py — retrieval metrics + paired bootstrap for the Item-4 ablation.

Pure-python (no numpy) so the metric math is trivially testable and matches the
stdlib-only spirit of benchmarks/significance.py. Every ranking metric takes a
ranked list of symbol ids (best first, query already excluded) and a relevant
set (binary relevance).
"""

import math
import random
from typing import Dict, Sequence, Set


def recall_at_k(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def mrr(ranked: Sequence[str], relevant: Set[str]) -> float:
    for i, d in enumerate(ranked, 1):
        if d in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    """Binary-relevance NDCG@k: DCG = sum 1/log2(rank+1) over hits in the top-k,
    normalized by the ideal ordering (all relevant first)."""
    dcg = sum(1.0 / math.log2(i + 1)
              for i, d in enumerate(ranked[:k], 1) if d in relevant)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def paired_bootstrap(a: Sequence[float], b: Sequence[float], n_boot: int = 10000,
                     seed: int = 0) -> Dict[str, float]:
    """Paired bootstrap over per-query scores. Returns the observed mean
    difference b-a, its 95% CI, and an approximate two-sided p (twice the
    smaller tail mass of the resampled mean crossing 0). Positive mean_diff
    means b beats a; a CI excluding 0 is the significance call."""
    diffs = [bi - ai for ai, bi in zip(a, b)]
    nq = len(diffs)
    if nq == 0:
        return {"mean_diff": 0.0, "ci_lo": 0.0, "ci_hi": 0.0, "p": 1.0, "n": 0}
    obs = sum(diffs) / nq
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(nq):
            s += diffs[rng.randrange(nq)]
        means.append(s / nq)
    means.sort()
    lo = means[int(0.025 * n_boot)]
    hi = means[min(int(0.975 * n_boot), n_boot - 1)]
    frac_le = sum(1 for m in means if m <= 0) / n_boot
    frac_ge = sum(1 for m in means if m >= 0) / n_boot
    p = min(1.0, 2 * min(frac_le, frac_ge))
    return {"mean_diff": obs, "ci_lo": lo, "ci_hi": hi, "p": p, "n": nq}
