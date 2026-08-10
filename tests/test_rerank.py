"""
tests/test_rerank.py — stage-2 reranker: the feature contract, pure-stdlib
inference, and the zero-dependency promise.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.models import RepositoryIndex, Symbol
from diffcontext.rerank.features import (
    FEATURE_NAMES,
    N_FEATURES,
    QueryContext,
    extract_features,
    split_identifier,
    _dir_distance,
)
from diffcontext.rerank.model import RerankModel, RerankModelError, _sigmoid


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _sym(sid, code):
    return Symbol(id=sid, file=sid.split(":")[0], name=sid.split(":")[1], code=code)


@pytest.fixture
def tiny():
    """A 5-symbol repo: a.py has a chain, b.py has an unrelated helper."""
    symbols = {
        "./a.py:handler": _sym("./a.py:handler", "def handler(req):\n    return parse_body(req)"),
        "./a.py:parse_body": _sym("./a.py:parse_body", "def parse_body(req):\n    return req.body"),
        "./a.py:Router.dispatch": _sym("./a.py:Router.dispatch", "def dispatch(self):\n    return handler(self.req)"),
        "./b.py:unrelated": _sym("./b.py:unrelated", "def unrelated():\n    return 42"),
        "./b.py:_private": _sym("./b.py:_private", "def _private():\n    return None"),
    }
    graph = {
        "./a.py:handler": ["./a.py:parse_body"],
        "./a.py:Router.dispatch": ["./a.py:handler"],
        "./b.py:unrelated": [],
        "./a.py:parse_body": [],
        "./b.py:_private": [],
    }
    reverse = {
        "./a.py:parse_body": {"./a.py:handler"},
        "./a.py:handler": {"./a.py:Router.dispatch"},
    }
    return symbols, graph, reverse


def _ctx(tiny, changed=("./a.py:handler",), **kw):
    symbols, graph, reverse = tiny
    return QueryContext(symbols, graph, reverse, list(changed), **kw)


def _weights(**over):
    blob = {
        "version": 1,
        "feature_names": list(FEATURE_NAMES),
        "mean": [0.0] * N_FEATURES,
        "scale": [1.0] * N_FEATURES,
        "coef": [0.0] * N_FEATURES,
        "intercept": 0.0,
    }
    blob.update(over)
    return blob


# ---------------------------------------------------------------------------
# Feature contract
# ---------------------------------------------------------------------------

def test_feature_names_are_unique_and_sized():
    assert len(FEATURE_NAMES) == N_FEATURES == 22
    assert len(set(FEATURE_NAMES)) == N_FEATURES


def test_extract_returns_fixed_width_finite_floats(tiny):
    ctx = _ctx(tiny)
    for cand in tiny[0]:
        if cand == "./a.py:handler":
            continue
        v = extract_features(ctx, cand)
        assert len(v) == N_FEATURES
        assert all(isinstance(x, float) for x in v)
        assert all(x == x and abs(x) != float("inf") for x in v), cand


def test_graph_geometry_features(tiny):
    ctx = _ctx(tiny)
    f = dict(zip(FEATURE_NAMES, extract_features(ctx, "./a.py:parse_body")))
    # handler -> parse_body is one forward hop
    assert f["inv_hop_fwd"] == pytest.approx(0.5)
    assert f["is_direct_callee"] == 1.0
    assert f["is_direct_caller"] == 0.0
    assert f["is_same_file"] == 1.0

    f = dict(zip(FEATURE_NAMES, extract_features(ctx, "./a.py:Router.dispatch")))
    # Router.dispatch -> handler, so dispatch is a *caller* of the change
    assert f["inv_hop_bwd"] == pytest.approx(0.5)
    assert f["is_direct_caller"] == 1.0
    assert f["is_direct_callee"] == 0.0


def test_unreachable_symbol_scores_zero_hops(tiny):
    ctx = _ctx(tiny)
    f = dict(zip(FEATURE_NAMES, extract_features(ctx, "./b.py:unrelated")))
    assert f["inv_hop_fwd"] == 0.0
    assert f["inv_hop_bwd"] == 0.0
    assert f["inv_hop_undirected"] == 0.0
    assert f["is_same_file"] == 0.0


def test_name_flags(tiny):
    ctx = _ctx(tiny)
    f = dict(zip(FEATURE_NAMES, extract_features(ctx, "./b.py:_private")))
    assert f["is_private"] == 1.0
    assert f["is_dunder"] == 0.0


def test_same_class_requires_same_file_and_class():
    symbols = {
        "./a.py:C.one": _sym("./a.py:C.one", "def one(self): pass"),
        "./a.py:C.two": _sym("./a.py:C.two", "def two(self): pass"),
        "./a.py:D.three": _sym("./a.py:D.three", "def three(self): pass"),
        "./b.py:C.four": _sym("./b.py:C.four", "def four(self): pass"),
    }
    ctx = QueryContext(symbols, {}, {}, ["./a.py:C.one"])
    def sc(cid):
        return dict(zip(FEATURE_NAMES, extract_features(ctx, cid)))["is_same_class"]
    assert sc("./a.py:C.two") == 1.0
    assert sc("./a.py:D.three") == 0.0
    assert sc("./b.py:C.four") == 0.0      # same class name, different file


def test_optional_signals_default_to_zero(tiny):
    """A missing signal must read as 'no evidence', never as a different scale."""
    ctx = _ctx(tiny)      # no import_maps, no cochange
    f = dict(zip(FEATURE_NAMES, extract_features(ctx, "./b.py:unrelated")))
    assert f["import_overlap"] == 0.0
    assert f["cochange_assoc"] == 0.0


def test_cochange_is_read_when_supplied(tiny):
    ctx = _ctx(tiny, cochange={"./b.py": 0.75})
    f = dict(zip(FEATURE_NAMES, extract_features(ctx, "./b.py:unrelated")))
    assert f["cochange_assoc"] == pytest.approx(0.75)


def test_bm25_rank_is_query_comparable(tiny):
    """Raw BM25 magnitude varies per query; the rank feature must not."""
    ctx = _ctx(tiny, bm25_scores={"./b.py:unrelated": 9.0, "./a.py:parse_body": 3.0})
    top = dict(zip(FEATURE_NAMES, extract_features(ctx, "./b.py:unrelated")))
    second = dict(zip(FEATURE_NAMES, extract_features(ctx, "./a.py:parse_body")))
    assert top["inv_bm25_rank"] == pytest.approx(1.0)
    assert second["inv_bm25_rank"] == pytest.approx(0.5)
    # A symbol BM25 never scored is 0.0, not the worst finite rank.
    absent = dict(zip(FEATURE_NAMES, extract_features(ctx, "./b.py:_private")))
    assert absent["inv_bm25_rank"] == 0.0


def test_split_identifier_camel_and_snake():
    assert split_identifier("send_request") == {"send", "request"}
    assert split_identifier("sendRequest") == {"send", "request"}
    assert split_identifier("HTTPAdapter.send") == {"http", "adapter", "send"}


def test_dir_distance():
    assert _dir_distance("./a/b.py", "./a/c.py") == 0.0
    assert _dir_distance("./a/b.py", "./a/d/c.py") == 1.0
    assert _dir_distance("./x/b.py", "./y/c.py") == 2.0


def test_token_cache_is_shared_and_correct(tiny):
    cache = {}
    ctx = _ctx(tiny, token_cache=cache)
    a = extract_features(ctx, "./a.py:parse_body")
    assert cache            # populated
    ctx2 = _ctx(tiny, token_cache=cache)
    b = extract_features(ctx2, "./a.py:parse_body")
    assert a == b           # cache must not change the answer


# ---------------------------------------------------------------------------
# Model loading + scoring
# ---------------------------------------------------------------------------

def test_sigmoid_is_stable_at_extremes():
    assert _sigmoid(0.0) == pytest.approx(0.5)
    assert _sigmoid(1000.0) == pytest.approx(1.0)
    assert _sigmoid(-1000.0) == pytest.approx(0.0)


def test_score_vector_matches_closed_form():
    m = RerankModel(FEATURE_NAMES, [0.0] * N_FEATURES, [2.0] * N_FEATURES,
                    [1.0] + [0.0] * (N_FEATURES - 1), 0.5)
    x = [4.0] + [0.0] * (N_FEATURES - 1)
    assert m.score_vector(x) == pytest.approx(_sigmoid(4.0 / 2.0 + 0.5))


def test_reordered_features_are_rejected(tmp_path):
    bad = _weights(feature_names=list(reversed(FEATURE_NAMES)))
    p = tmp_path / "w.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(RerankModelError, match="feature contract mismatch"):
        RerankModel.load(str(p))


def test_zero_scale_is_rejected(tmp_path):
    bad = _weights(scale=[0.0] + [1.0] * (N_FEATURES - 1))
    p = tmp_path / "w.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(RerankModelError, match="zero scale"):
        RerankModel.load(str(p))


def test_version_mismatch_is_rejected(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps(_weights(version=99)))
    with pytest.raises(RerankModelError, match="version"):
        RerankModel.load(str(p))


def test_missing_weights_raise_actionable_error(tmp_path):
    with pytest.raises(RerankModelError, match="no reranker weights"):
        RerankModel.load(str(tmp_path / "nope.json"))


def test_malformed_json_is_rejected(tmp_path):
    p = tmp_path / "w.json"
    p.write_text("{not json")
    with pytest.raises(RerankModelError, match="malformed"):
        RerankModel.load(str(p))


def test_rerank_ties_preserve_stage1_order(tiny):
    """An all-zero model must degrade to the shipped ranking, not scramble it."""
    m = RerankModel(FEATURE_NAMES, [0.0] * N_FEATURES, [1.0] * N_FEATURES,
                    [0.0] * N_FEATURES, 0.0)
    ctx = _ctx(tiny)
    pool = ["./a.py:parse_body", "./b.py:unrelated", "./a.py:Router.dispatch"]
    assert m.rerank(ctx, pool) == pool


def test_rerank_orders_by_probability(tiny):
    idx = list(FEATURE_NAMES).index("is_direct_callee")
    coef = [0.0] * N_FEATURES
    coef[idx] = 5.0
    m = RerankModel(FEATURE_NAMES, [0.0] * N_FEATURES, [1.0] * N_FEATURES, coef, 0.0)
    ctx = _ctx(tiny)
    pool = ["./b.py:unrelated", "./a.py:parse_body"]
    assert m.rerank(ctx, pool)[0] == "./a.py:parse_body"


def test_wrong_width_vector_is_rejected():
    m = RerankModel(FEATURE_NAMES, [0.0] * N_FEATURES, [1.0] * N_FEATURES,
                    [0.0] * N_FEATURES, 0.0)
    with pytest.raises(RerankModelError, match="expected 22 features"):
        m.score_vector([1.0, 2.0])


# ---------------------------------------------------------------------------
# The zero-dependency promise
# ---------------------------------------------------------------------------

def test_inference_never_imports_numpy(tmp_path):
    """`pyproject.toml` declares `dependencies = []`. Reranking at inference
    time must therefore stay pure stdlib — this test fails loudly the moment
    someone reaches for numpy inside the package."""
    weights = tmp_path / "w.json"
    weights.write_text(json.dumps(_weights(coef=[0.5] * N_FEATURES)))
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {root!r})
        from diffcontext.rerank.model import RerankModel
        from diffcontext.rerank.features import QueryContext, extract_features
        from diffcontext.models import Symbol

        s = {{"./a.py:f": Symbol(id="./a.py:f", file="./a.py", name="f", code="def f(): pass"),
              "./a.py:g": Symbol(id="./a.py:g", file="./a.py", name="g", code="def g(): return f()")}}
        ctx = QueryContext(s, {{"./a.py:g": ["./a.py:f"]}}, {{"./a.py:f": {{"./a.py:g"}}}}, ["./a.py:g"])
        m = RerankModel.load({str(weights)!r})
        assert 0.0 <= m.score_candidates(ctx, ["./a.py:f"])["./a.py:f"] <= 1.0
        assert m.rerank(ctx, ["./a.py:f"]) == ["./a.py:f"]

        leaked = sorted(x for x in ("numpy", "scipy", "torch", "sklearn", "pandas")
                        if x in sys.modules)
        print("LEAKED:" + ",".join(leaked))
    """)
    r = subprocess.run([sys.executable, "-c", script],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("LEAKED:"))
    leaked = [x for x in line[len("LEAKED:"):].split(",") if x]
    assert leaked == [], f"inference pulled in third-party modules: {leaked}"


def test_shipped_weights_load_if_present():
    """If weights.json is committed, it must satisfy the current contract."""
    from diffcontext.rerank.model import WEIGHTS_PATH
    if not os.path.exists(WEIGHTS_PATH):
        pytest.skip("no weights.json committed yet")
    m = RerankModel.load()
    assert m.feature_names == tuple(FEATURE_NAMES)
    assert len(m.coef) == N_FEATURES
