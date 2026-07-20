"""
tests/test_cutoff.py — the largest-gap dynamic cutoff (`--cutoff gap`).

The gap cutoff is the measured precision lever from the 2026-07 rigor pass
(benchmarks/RIGOR_REPORT_2026-07.md §7): cut the ranking at the largest
relative score drop within the top 50 instead of keeping a fixed top-k.
These tests pin the product implementation to the exact semantics measured
in benchmarks/blend_loro.py eval_cutoff_policies:

  - fewer than 3 positive candidates -> keep everything (no distribution)
  - otherwise cut AFTER position argmax(score[i]/score[i+1]), first max wins
  - only positive-score candidates are retrievable under the policy
  - the policy applies BEFORE top_k and the token budget
  - changed symbols are never subject to the cutoff
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.context.selector import gap_cut_count, select_context
from diffcontext.models import Symbol


def _symbols(ids):
    return {
        sid: Symbol(id=sid, file=sid.split(":")[0], name=sid.split(":")[1],
                    code=f"def {sid.split(':')[1]}():\n    return 1\n")
        for sid in ids
    }


# ---------------------------------------------------------------------------
# gap_cut_count — the policy itself
# ---------------------------------------------------------------------------

def test_cut_lands_at_the_largest_relative_drop():
    # 100 -> 90 (1.11x), 90 -> 10 (9x), 10 -> 8 (1.25x): cut after the 90.
    assert gap_cut_count([100.0, 90.0, 10.0, 8.0]) == 2


def test_relative_not_absolute_drop():
    # Absolute drops: 60 then 3.9; relative: 2.5x then 39x. The relative
    # rule cuts at the tail drop, not the big absolute one at the head.
    assert gap_cut_count([100.0, 40.0, 4.0, 0.1]) == 3


def test_fewer_than_three_candidates_keeps_all():
    assert gap_cut_count([]) == 0
    assert gap_cut_count([50.0]) == 1
    assert gap_cut_count([50.0, 1.0]) == 2


def test_first_maximal_gap_wins_on_ties():
    # 100->10 and 10->1 are both 10x; benchmark used np.argmax = first.
    assert gap_cut_count([100.0, 10.0, 1.0]) == 1


def test_gap_beyond_window_is_ignored():
    # A huge drop past the top-50 window must not attract the cut.
    scores = [1000.0 * (0.99 ** i) for i in range(60)]
    scores[54] = scores[53] / 100.0
    cut = gap_cut_count(scores)
    assert cut <= 49  # the cut can only land inside the window


# ---------------------------------------------------------------------------
# select_context(cutoff="gap") — integration with the selector
# ---------------------------------------------------------------------------

def test_gap_cutoff_drops_below_the_gap_and_discloses():
    syms = _symbols(
        ["./a.py:changed", "./a.py:s1", "./a.py:s2", "./b.py:s3", "./b.py:s4"]
    )
    scores = {
        "./a.py:changed": 100.0,
        "./a.py:s1": 95.0,
        "./a.py:s2": 90.0,   # 95/90 = 1.06x
        "./b.py:s3": 9.0,    # 90/9 = 10x  <- cut lands here
        "./b.py:s4": 8.0,
    }
    selected, dropped = select_context(
        syms, scores, ["./a.py:changed"], cutoff="gap",
    )
    assert selected == ["./a.py:changed", "./a.py:s1", "./a.py:s2"]
    # Cut symbols are disclosed as dropped, not silently vanished.
    assert set(dropped) == {"./b.py:s3", "./b.py:s4"}


def test_zero_score_candidates_never_retrieved_under_gap():
    syms = _symbols(["./a.py:changed", "./a.py:s1", "./a.py:s2", "./a.py:s3"])
    scores = {
        "./a.py:changed": 100.0,
        "./a.py:s1": 50.0,
        "./a.py:s2": 45.0,
        "./a.py:s3": 0.0,   # only 2 positive candidates -> both kept
    }
    selected, dropped = select_context(
        syms, scores, ["./a.py:changed"], cutoff="gap",
    )
    assert "./a.py:s3" not in selected
    assert "./a.py:s3" in dropped
    assert "./a.py:s1" in selected and "./a.py:s2" in selected


def test_changed_symbols_immune_to_cutoff():
    syms = _symbols(["./a.py:changed", "./a.py:s1"])
    # The changed symbol would fall far below any gap; it stays regardless.
    scores = {"./a.py:changed": 0.0, "./a.py:s1": 50.0}
    selected, _ = select_context(syms, scores, ["./a.py:changed"], cutoff="gap")
    assert "./a.py:changed" in selected


def test_gap_applies_before_top_k_and_budget():
    syms = _symbols(["./a.py:c", "./a.py:s1", "./a.py:s2", "./a.py:s3"])
    scores = {
        "./a.py:c": 100.0,
        "./a.py:s1": 80.0,
        "./a.py:s2": 8.0,   # 10x drop: gap keeps only s1
        "./a.py:s3": 7.0,
    }
    # top_k=3 alone would keep s1, s2, s3; gap tightens it to s1.
    selected, dropped = select_context(
        syms, scores, ["./a.py:c"], top_k=3, cutoff="gap",
    )
    assert selected == ["./a.py:c", "./a.py:s1"]
    assert set(dropped) == {"./a.py:s2", "./a.py:s3"}


def test_default_behavior_unchanged_without_cutoff():
    syms = _symbols(["./a.py:c", "./a.py:s1", "./a.py:s2", "./a.py:s3"])
    scores = {
        "./a.py:c": 100.0,
        "./a.py:s1": 80.0,
        "./a.py:s2": 8.0,
        "./a.py:s3": 0.0,   # zero-score symbols ARE retrievable without a cutoff
    }
    selected, dropped = select_context(syms, scores, ["./a.py:c"])
    assert selected == ["./a.py:c", "./a.py:s1", "./a.py:s2", "./a.py:s3"]
    assert dropped == []


def test_unknown_cutoff_rejected():
    syms = _symbols(["./a.py:c"])
    with pytest.raises(ValueError, match="cutoff"):
        select_context(syms, {"./a.py:c": 1.0}, ["./a.py:c"], cutoff="mass")
