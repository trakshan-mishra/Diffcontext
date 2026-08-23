"""
tests/test_stats.py — Statistical functions for ContextBench pass@1 reporting.

Pins the three pure functions in benchmarks/contextbench/stats.py:
  - wilson_interval: Wilson score 95% CI for a binomial proportion
  - mcnemar_exact:   Exact two-sided McNemar test
  - paired_table:    2x2 concordance table from paired results

The key regression test is mcnemar_exact(11, 8) ≈ 0.648 — the gap-vs-depboost
pair from the full 128-task GLM 5.2 run (see RESULTS.md section 3).
"""

import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from benchmarks.contextbench.stats import (
    wilson_interval, mcnemar_exact, paired_table,
)


class TestWilsonInterval:
    def test_basic_proportion(self):
        lo, hi = wilson_interval(7, 128)
        assert 0.025 < lo < 0.030   # ~0.027
        assert 0.105 < hi < 0.112   # ~0.109

    def test_half_success(self):
        lo, hi = wilson_interval(50, 100)
        assert abs(lo - 0.404) < 0.005
        assert abs(hi - 0.596) < 0.005

    def test_zero_n(self):
        lo, hi = wilson_interval(0, 0)
        assert lo == 0.0 and hi == 1.0

    def test_all_success(self):
        lo, hi = wilson_interval(128, 128)
        assert lo > 0.95
        assert hi == 1.0

    def test_zero_success(self):
        lo, hi = wilson_interval(0, 128)
        assert lo == 0.0
        assert hi < 0.05

    def test_33_of_128(self):
        """The gap variant's headline number."""
        lo, hi = wilson_interval(33, 128)
        assert 0.188 < lo < 0.192   # ~0.190
        assert 0.338 < hi < 0.342   # ~0.340

    def test_bounds_within_unit(self):
        for k in range(0, 21):
            lo, hi = wilson_interval(k, 20)
            assert 0.0 <= lo <= hi <= 1.0


class TestMcNemarExact:
    def test_symmetric(self):
        """b == c -> p = 1.0 (no evidence of difference)."""
        assert mcnemar_exact(10, 10) == 1.0

    def test_gap_vs_depboost(self):
        """The key regression: gap_only=11, depboost_only=8 -> p ≈ 0.648."""
        p = mcnemar_exact(11, 8)
        assert abs(p - 0.648) < 0.005

    def test_none_vs_gap(self):
        """none_only=2, gap_only=28 -> highly significant."""
        p = mcnemar_exact(2, 28)
        assert p < 0.0001

    def test_none_vs_diffcontext(self):
        """none_only=2, diffcontext_only=23 -> highly significant."""
        p = mcnemar_exact(2, 23)
        assert p < 0.0005

    def test_diffcontext_vs_gap(self):
        """diffcontext_only=7, gap_only=12 -> p ≈ 0.359."""
        p = mcnemar_exact(7, 12)
        assert abs(p - 0.359) < 0.01

    def test_diffcontext_vs_depboost(self):
        """diffcontext_only=8, depboost_only=10 -> p ≈ 0.815."""
        p = mcnemar_exact(8, 10)
        assert abs(p - 0.815) < 0.01

    def test_zero_discordant(self):
        """No discordant pairs -> p = 1.0."""
        assert mcnemar_exact(0, 0) == 1.0

    def test_one_sided_extreme(self):
        """All discordant one way -> very small p."""
        p = mcnemar_exact(0, 20)
        assert p < 0.0001

    def test_two_sided_symmetry(self):
        """b=N, c=0 should equal b=0, c=N (symmetric under H0)."""
        assert abs(mcnemar_exact(20, 0) - mcnemar_exact(0, 20)) < 1e-12


class TestPairedTable:
    def test_basic(self):
        a = {"x": True, "y": True, "z": False, "w": False}
        b = {"x": True, "y": False, "z": True, "w": False}
        both, a_only, b_only, neither = paired_table(a, b)
        assert both == 1      # x
        assert a_only == 1    # y
        assert b_only == 1    # z
        assert neither == 1   # w

    def test_intersection_only(self):
        """Instances in only one set are excluded."""
        a = {"x": True, "y": False, "extra1": True}
        b = {"x": False, "y": False, "extra2": True}
        both, a_only, b_only, neither = paired_table(a, b)
        assert both == 0
        assert a_only == 1    # x
        assert b_only == 0
        assert neither == 1   # y

    def test_empty(self):
        both, a_only, b_only, neither = paired_table({}, {})
        assert (both, a_only, b_only, neither) == (0, 0, 0, 0)

    def test_all_pass_both(self):
        a = {"x": True, "y": True}
        b = {"x": True, "y": True}
        both, a_only, b_only, neither = paired_table(a, b)
        assert (both, a_only, b_only, neither) == (2, 0, 0, 0)

    def test_all_fail_both(self):
        a = {"x": False, "y": False}
        b = {"x": False, "y": False}
        both, a_only, b_only, neither = paired_table(a, b)
        assert (both, a_only, b_only, neither) == (0, 0, 0, 2)
