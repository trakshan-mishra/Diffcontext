"""Correctness of the retrieval metrics and the paired bootstrap."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.semantic.metrics import mrr, ndcg_at_k, paired_bootstrap, recall_at_k


def test_recall_at_k():
    assert recall_at_k(["a", "b", "c"], {"a", "x"}, 3) == 0.5      # 1 of 2 found
    assert recall_at_k(["a", "b"], {"a", "b"}, 1) == 0.5           # k cuts before b
    assert recall_at_k(["a"], set(), 3) == 0.0                     # no relevant


def test_mrr():
    assert mrr(["x", "rel", "y"], {"rel"}) == 0.5                  # rank 2
    assert mrr(["rel"], {"rel"}) == 1.0
    assert mrr(["a", "b"], {"z"}) == 0.0                           # miss


def test_ndcg():
    assert ndcg_at_k(["rel"], {"rel"}, 10) == 1.0                  # perfect
    # single relevant at rank 3: DCG=1/log2(4), IDCG=1/log2(2)=1
    assert abs(ndcg_at_k(["x", "y", "rel"], {"rel"}, 3) - 1 / math.log2(4)) < 1e-9
    assert ndcg_at_k(["x"], set(), 5) == 0.0


def test_paired_bootstrap_detects_and_nulls():
    strong = paired_bootstrap([0.0] * 30, [1.0] * 30, n_boot=2000)
    assert abs(strong["mean_diff"] - 1.0) < 1e-9
    assert strong["ci_lo"] > 0 and strong["p"] < 0.05             # b clearly beats a
    null = paired_bootstrap([0.5] * 20, [0.5] * 20, n_boot=2000)
    assert null["mean_diff"] == 0.0
    assert null["ci_lo"] <= 0 <= null["ci_hi"]                    # no difference
