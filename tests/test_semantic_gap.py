"""Unit tests for the adversarial-gap builder — repo-free (synthetic index).

Locks the two things that make Item 3 a fair test: relatedness comes from real
graph edges (never from token similarity), and the adversarial cut keeps the
low-lexical-overlap tail.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from diffcontext.models import RepositoryIndex, Symbol

from benchmarks.semantic.adversarial_gap import (
    build_gap_set, jaccard, name_tokens, tokenize_code,
)


def test_tokenize_splits_and_filters():
    toks = tokenize_code("def saveUser(self):\n    return check_password(user_id)")
    assert {"save", "user", "check", "password"} <= toks
    assert "self" not in toks and "def" not in toks and "id" not in toks   # stop/short


def test_name_tokens():
    assert name_tokens("./a.py:Account.save_user") == {"account", "save", "user"}


def test_jaccard_basic():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard({"a"}, {"b"}) == 0.0
    assert jaccard(set(), {"a"}) == 0.0
    assert abs(jaccard({"a", "b"}, {"b", "c"}) - 1 / 3) < 1e-9


def _sym(sid, code):
    return Symbol(id=sid, file=sid.split(":", 1)[0], name=sid.split(":", 1)[1], code=code)


def test_gap_set_uses_edges_not_similarity():
    # save_user -> check_password: a REAL edge, disjoint vocabulary (adversarial).
    # save_user -> save_account: NO edge but high word overlap (must NOT appear).
    syms = {
        "./a.py:save_user": _sym("./a.py:save_user", "def save_user(record): return store(record)"),
        "./b.py:check_password": _sym("./b.py:check_password", "def check_password(digest): return verify(digest)"),
        "./a.py:save_account": _sym("./a.py:save_account", "def save_account(record): return store(record)"),
    }
    graph = {"./a.py:save_user": ["./b.py:check_password"]}   # the only real edge
    index = RepositoryIndex(symbols=syms, graph=graph)

    gap, stats = build_gap_set(index, "toy", percentile=100.0)  # keep all real edges
    pairs = {(p.query_symbol, p.gt_symbol) for p in gap}
    assert ("./a.py:save_user", "./b.py:check_password") in pairs       # real edge kept
    # the lexically-similar NON-edge is absent — relatedness is the edge, not words
    assert ("./a.py:save_user", "./a.py:save_account") not in pairs
    edge = next(p for p in gap if p.query_symbol == "./a.py:save_user")
    assert edge.edge_dir == "callee"
    assert edge.cross_file is True
    assert edge.body_jaccard == 0.0        # disjoint vocabulary, as designed
    # the same edge is also a query case from the callee's side (its caller)
    assert ("./b.py:check_password", "./a.py:save_user") in pairs
    assert stats["n_edge_pairs"] == 2


def test_gap_cut_keeps_low_overlap_tail():
    # two real edges: one word-different (low J), one word-similar (high J).
    syms = {
        "./m.py:q": _sym("./m.py:q", "def q(): return alpha_beta_gamma()"),
        "./m.py:low": _sym("./m.py:low", "def low(): return zebra_yak_wolf()"),   # 0 overlap
        "./m.py:high": _sym("./m.py:high", "def high(): return alpha_beta_delta()"),  # shares alpha,beta
    }
    graph = {"./m.py:q": ["./m.py:low", "./m.py:high"]}
    index = RepositoryIndex(symbols=syms, graph=graph)
    gap, _ = build_gap_set(index, "toy", percentile=50.0)   # bottom half only
    kept = {p.gt_symbol for p in gap}
    assert "./m.py:low" in kept          # the low-overlap edge survives the cut
    assert "./m.py:high" not in kept     # the high-overlap edge is filtered out


def test_cochange_annotation():
    syms = {
        "./a.py:q": _sym("./a.py:q", "def q(): return foo_bar_baz()"),
        "./b.py:g": _sym("./b.py:g", "def g(): return alpha_beta_gamma()"),
    }
    index = RepositoryIndex(symbols=syms, graph={"./a.py:q": ["./b.py:g"]})
    cc = {frozenset(("./a.py:q", "./b.py:g"))}
    gap, _ = build_gap_set(index, "toy", percentile=100.0, cochange=cc)
    assert gap and all(p.co_changed for p in gap)
