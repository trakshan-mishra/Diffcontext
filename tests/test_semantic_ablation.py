"""End-to-end ablation on a synthetic index with FAKE vectors — proves the
harness (three arms + metrics) works before real CodeXEmbed vectors exist.

The scenario is the Item-3 gap in miniature: the relevant symbol is a real graph
neighbor of the query but lies FAR from it in vector space. Structural must find
it, semantic must miss it at k=1, and the hybrid must recover it — the whole
thesis of combining the two signals.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from diffcontext.models import RepositoryIndex, Symbol

from benchmarks.semantic.ablation import rank_semantic, rank_structural, rrf, run_repo


def _sym(sid, code):
    return Symbol(id=sid, file=sid.split(":", 1)[0], name=sid.split(":", 1)[1], code=code)


def _index():
    syms = {
        "./a.py:q": _sym("./a.py:q", "def q(): return partner()"),
        "./b.py:partner": _sym("./b.py:partner", "def partner(): return 1"),   # graph neighbor
        "./c.py:decoy": _sym("./c.py:decoy", "def decoy(): return 2"),          # vector-near, unrelated
    }
    return RepositoryIndex(symbols=syms, graph={"./a.py:q": ["./b.py:partner"]})


def _vectors():
    # q close to decoy, far from its true partner -> the adversarial case
    return {
        "./a.py:q": np.array([1.0, 0.0], dtype="float32"),
        "./c.py:decoy": np.array([0.98, 0.20], dtype="float32"),
        "./b.py:partner": np.array([0.0, 1.0], dtype="float32"),
    }


def test_rrf_promotes_agreed_and_structural_hits():
    fused = rrf([["decoy", "partner"], ["partner"]])
    assert fused[0] == "partner"          # ranked #1 by structural lifts it to the top


def test_semantic_misses_structural_catches_hybrid_recovers():
    index, id2vec = _index(), _vectors()
    recs = run_repo(index, id2vec, {"./a.py:q": {"./b.py:partner"}}, k=1)
    assert len(recs) == 1
    r = recs[0]
    assert r["semantic"]["recall"] == 0.0      # decoy outranks the true partner at k=1
    assert r["structural"]["recall"] == 1.0    # graph edge finds it
    assert r["hybrid"]["recall"] == 1.0        # fusion recovers the structural hit
    # every metric stays in [0, 1]
    for arm in ("semantic", "structural", "hybrid"):
        for m in ("ndcg", "mrr", "recall"):
            assert 0.0 <= r[arm][m] <= 1.0


def test_run_repo_skips_queries_with_no_scorable_relevant():
    index, id2vec = _index(), _vectors()
    # relevant symbol not in the corpus -> query dropped, no records
    assert run_repo(index, id2vec, {"./a.py:q": {"./z.py:ghost"}}, k=1) == []


def test_semantic_and_structural_rankings_exclude_query():
    index, id2vec = _index(), _vectors()
    corpus = [i for i in index.symbols if i in id2vec]
    matrix = np.stack([id2vec[i] for i in corpus])
    assert "./a.py:q" not in rank_semantic("./a.py:q", corpus, matrix, id2vec)
    assert "./a.py:q" not in rank_structural(index, "./a.py:q")
