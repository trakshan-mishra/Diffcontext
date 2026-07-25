"""Unit tests for the audit sampler and stats — repo-free (injected resolver).

Locks the sampling contract (exact size, determinism, alive-only links,
cross-file/bucket tagging) and the two statistics that carry the validity story:
Wilson CI and Cohen's kappa.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.semantic.audit_sample import build_links, gt_bucket, stratified_sample
from benchmarks.semantic.audit_stats import cohen_kappa, precision, wilson


# ---- fixtures ----------------------------------------------------------------

_CODE = {
    ("r", "./a.py:f"): "def f(): pass",
    ("r", "./a.py:g"): "def g(): pass",
    ("r", "./b.py:h"): "def h(): pass",
    # ./b.py:missing intentionally absent -> not alive at HEAD
}


def _resolver(repo, sym):
    return _CODE.get((repo, sym))


def _pair(query, gts):
    return {"repo": "r", "commit": "0123456789abcdef", "commit_msg": "m",
            "query_symbol": query, "gt_symbols": gts}


# ---- sampler -----------------------------------------------------------------

def test_build_links_drops_unresolvable_and_tags_correctly():
    pairs = [_pair("./a.py:f", ["./a.py:g", "./b.py:h", "./b.py:missing"])]
    links = build_links(pairs, _resolver)
    # ./b.py:missing has no HEAD code -> dropped; two links survive
    assert {lk.gt_symbol for lk in links} == {"./a.py:g", "./b.py:h"}
    by_gt = {lk.gt_symbol: lk for lk in links}
    assert by_gt["./a.py:g"].cross_file is False     # same file
    assert by_gt["./b.py:h"].cross_file is True       # different file
    assert all(lk.gt_size == 3 for lk in links)         # gt_size is the pair's set size
    assert all(lk.gt_bucket == "md" for lk in links)


def test_build_links_skips_dead_query():
    links = build_links([_pair("./b.py:missing", ["./a.py:f"])], _resolver)
    assert links == []


def test_gt_bucket_boundaries():
    assert [gt_bucket(n) for n in (1, 2, 3, 5, 6, 20)] == ["sm", "sm", "md", "md", "lg", "lg"]


def _many_links(n_queries, gt_per):
    code = {}
    pairs = []
    for i in range(n_queries):
        q = f"./a.py:q{i}"
        code[("r", q)] = "x"
        gts = [f"./b{j}.py:g{i}_{j}" for j in range(gt_per)]
        for g in gts:
            code[("r", g)] = "y"
        pairs.append({"repo": "r", "commit": f"c{i:04d}", "commit_msg": "m",
                      "query_symbol": q, "gt_symbols": gts})
    return build_links(pairs, lambda repo, s: code.get((repo, s)))


def test_stratified_sample_query_capped_and_deterministic():
    links = _many_links(60, 5)            # 300 links across 60 equal-weight queries
    a = stratified_sample(links, 30, seed=1, links_per_query=3)
    b = stratified_sample(links, 30, seed=1, links_per_query=3)
    assert [lk.link_id for lk in a] == [lk.link_id for lk in b]        # deterministic
    assert len(a) == 30                                            # 10 queries x 3 links
    from collections import Counter
    per_query = Counter((lk.repo, lk.commit, lk.query_symbol) for lk in a)
    assert max(per_query.values()) <= 3                           # per-query cap honored
    assert stratified_sample(links, 30, seed=2, links_per_query=3) != a   # seed matters


def test_stratified_sample_never_exceeds_n():
    links = _many_links(60, 5)
    assert len(stratified_sample(links, 25, seed=0, links_per_query=3)) <= 25


def test_stratified_sample_returns_all_when_small():
    links = build_links([_pair("./a.py:f", ["./a.py:g"])], _resolver)
    assert len(stratified_sample(links, 100, seed=0)) == 1


# ---- stats -------------------------------------------------------------------

def test_wilson_point_and_bounds():
    p, lo, hi = wilson(8, 10)
    assert abs(p - 0.8) < 1e-9
    assert 0.0 < lo < 0.8 < hi < 1.0
    assert wilson(0, 0) == (0.0, 0.0, 0.0)


def test_precision_ignores_unsure():
    rel, inc, (p, _, _) = precision(["related", "related", "incidental", "unsure"])
    assert (rel, inc) == (2, 1)
    assert abs(p - 2 / 3) < 1e-9


def test_cohen_kappa_perfect_and_known():
    labels = ["related", "incidental", "related"]
    k, po = cohen_kappa(labels, labels)
    assert abs(k - 1.0) < 1e-9 and abs(po - 1.0) < 1e-9
    # hand-computed: po=0.75, pe=0.5 -> kappa=0.5
    a = ["related", "related", "incidental", "incidental"]
    b = ["related", "incidental", "incidental", "incidental"]
    k, po = cohen_kappa(a, b)
    assert abs(po - 0.75) < 1e-9
    assert abs(k - 0.5) < 1e-9
