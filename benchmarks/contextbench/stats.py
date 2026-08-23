#!/usr/bin/env python3
"""
stats.py — Statistical functions for ContextBench pass@1 reporting.

Pure functions, no I/O. Used by verify_results.py and RESULTS.md
regeneration to produce confidence intervals and paired significance
tests without external dependencies.

- wilson_interval: Wilson score 95% CI for a binomial proportion
- mcnemar_exact:   Exact McNemar test (two-sided binomial) for paired
                   proportions
- paired_table:    Build a 2x2 concordance table from two variant result
                   sets keyed on instance_id
"""

import math
from typing import Dict, List, Optional, Tuple


def wilson_interval(k: int, n: int, alpha: float = 0.05) -> Tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion.

    Args:
        k: number of successes
        n: number of trials
        alpha: significance level (default 0.05 for 95% CI)

    Returns:
        (lower, upper) bounds as proportions in [0, 1].

    Better coverage than the normal approximation at small n and near
    0/1. Standard in clinical reporting; no continuity correction needed
    for the score interval.
    """
    if n == 0:
        return (0.0, 1.0)
    z = _z_score(1 - alpha / 2)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar test p-value.

    Args:
        b: count of discordant pairs where A succeeded and B failed
        c: count of discordant pairs where A failed and B succeeded

    Returns:
        Two-sided p-value from the exact binomial test on n = b + c
        discordant pairs under H0: p = 0.5.

    Uses the exact binomial (not the chi-square approximation), which is
    correct at small discordant counts where the chi-square approximation
    breaks down.
    """
    n = b + c
    if n == 0:
        return 1.0
    # Two-sided: 2 * P(X <= min(b, c)) for X ~ Binomial(n, 0.5)
    m = min(b, c)
    p = 2 * sum(math.comb(n, i) * 0.5 ** n for i in range(m + 1))
    return min(p, 1.0)


def paired_table(
    a_results: Dict[str, bool],
    b_results: Dict[str, bool],
) -> Tuple[int, int, int, int]:
    """Build a 2x2 concordance table from two variant result sets.

    Args:
        a_results: {instance_id: passed} for variant A
        b_results: {instance_id: passed} for variant B

    Returns:
        (both_pass, a_only, b_only, neither) where:
          both_pass: A and B both passed
          a_only:    A passed, B failed
          b_only:    A failed, B passed
          neither:   both failed

    Only instances present in BOTH result sets are counted (matched pairs).
    """
    common = set(a_results) & set(b_results)
    both = sum(1 for i in common if a_results[i] and b_results[i])
    a_only = sum(1 for i in common if a_results[i] and not b_results[i])
    b_only = sum(1 for i in common if not a_results[i] and b_results[i])
    neither = sum(1 for i in common if not a_results[i] and not b_results[i])
    return both, a_only, b_only, neither


def _z_score(p: float) -> float:
    """Inverse normal CDF (percent-point function) via rational approximation.

    Acklam's algorithm — accurate to ~1e-9 across the full range, no
    scipy dependency. p is the cumulative probability (e.g., 0.975 for
    the 97.5th percentile = 1.96).
    """
    # Coefficients for the rational approximation
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806617945e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288574e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]

    plow = 0.02425
    phigh = 1 - plow

    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    elif p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
