"""
tests/test_callee_bypass.py — the cross-file direct-neighbour bypass.

A symbol with a real call edge to a changed symbol, living in a DIFFERENT
file, is the one candidate whose relevance is structurally certain rather
than inferred. The hybrid blend still routinely ranks it below same-file
and lexical noise, and the budget then cuts it: the canonical case is
requests' `has_read`, a direct callee of `_encode_files` that ranked 26 of
247 while the package held 16.

The bypass reserves a small, capped number of front-of-queue slots for
those neighbours. What it deliberately does NOT do is exempt them from the
token budget. This module's own history is the reason: an earlier rule let
any score>=80 candidate bypass the budget, direct callees scored ~90 after
the structural bonus, and they consumed the whole budget before cheaper
co-change siblings were evaluated (see selector.py's header). Capped
promotion and budget exemption are different interventions; only the first
one is here.

Same-file neighbours are excluded on purpose — co-location already carries
them (~80% recall) and including them would spend the cap on symbols that
did not need it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.context.selector import select_context
from diffcontext.models import Symbol

REQUESTS_REPO = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "benchmark_repos", "requests",
)


def _symbols(ids, code_lines=1):
    body = "\n".join(f"    x{i} = {i}" for i in range(code_lines))
    return {
        sid: Symbol(id=sid, file=sid.split(":")[0], name=sid.split(":")[1],
                    code=f"def {sid.split(':')[1]}():\n{body}\n    return 1\n")
        for sid in ids
    }


def _crowded_ranking(n_noise=40):
    """A changed symbol, one cross-file callee ranked far down, and noise.

    Mirrors the has_read shape: the callee is structurally certain and
    lexically unremarkable, so it loses to same-file siblings.
    """
    changed = "./a.py:changed"
    callee = "./b.py:real_callee"
    noise = [f"./a.py:noise{i}" for i in range(n_noise)]
    symbols = _symbols([changed, callee] + noise)
    # Callee ranks below every noise symbol — the failure being fixed.
    scores = {changed: 100.0, callee: 5.0}
    scores.update({sid: 50.0 - i * 0.1 for i, sid in enumerate(noise)})
    graph = {changed: [callee]}
    return changed, callee, symbols, scores, graph


class TestCrossFileNeighbourBypass:
    def test_cross_file_callee_survives_a_top_k_that_would_cut_it(self):
        changed, callee, symbols, scores, graph = _crowded_ranking()
        selected, _ = select_context(
            symbols, scores, [changed], max_tokens=100000, top_k=5, graph=graph,
        )
        assert callee in selected

    def test_caller_direction_also_qualifies(self):
        """The edge is undirected for this purpose: a cross-file CALLER of
        the changed symbol is equally certain to be affected."""
        changed, caller, symbols, scores, _ = _crowded_ranking()
        graph = {caller: [changed]}          # caller -> changed
        selected, _ = select_context(
            symbols, scores, [changed], max_tokens=100000, top_k=5, graph=graph,
        )
        assert caller in selected

    def test_same_file_neighbour_is_not_bypassed(self):
        """Co-location already carries these; the cap is for cross-file."""
        changed = "./a.py:changed"
        sibling = "./a.py:sibling"
        noise = [f"./c.py:noise{i}" for i in range(10)]
        symbols = _symbols([changed, sibling] + noise)
        scores = {changed: 100.0, sibling: 1.0}
        scores.update({sid: 50.0 - i for i, sid in enumerate(noise)})
        selected, _ = select_context(
            symbols, scores, [changed], max_tokens=100000, top_k=3,
            graph={changed: [sibling]},
        )
        assert sibling not in selected

    def test_bypass_is_capped(self):
        """More neighbours than the cap: only `cap` of them are promoted,
        highest-scored first. An uncapped rule is the budget-eating bug
        this module removed once already."""
        changed = "./a.py:changed"
        callees = [f"./b.py:callee{i}" for i in range(8)]
        noise = [f"./a.py:noise{i}" for i in range(20)]
        symbols = _symbols([changed] + callees + noise)
        scores = {changed: 100.0}
        # callee0 best of the callees, callee7 worst
        scores.update({sid: 5.0 - i * 0.1 for i, sid in enumerate(callees)})
        scores.update({sid: 50.0 for sid in noise})
        selected, _ = select_context(
            symbols, scores, [changed], max_tokens=100000, top_k=10,
            graph={changed: callees}, neighbour_cap=3,
        )
        promoted = [c for c in callees if c in selected]
        assert promoted == callees[:3]

    def test_bypass_does_not_exempt_the_token_budget(self):
        """A promoted neighbour that does not fit is still dropped. This is
        the guardrail against re-introducing the score>=80 budget bypass."""
        changed = "./a.py:changed"
        callee = "./b.py:huge_callee"
        symbols = _symbols([changed], code_lines=1)
        symbols.update(_symbols([callee], code_lines=4000))
        scores = {changed: 100.0, callee: 5.0}
        selected, dropped = select_context(
            symbols, scores, [changed], max_tokens=300,
            graph={changed: [callee]},
        )
        assert callee not in selected
        assert callee in dropped

    def test_disabled_by_cap_zero(self):
        changed, callee, symbols, scores, graph = _crowded_ranking()
        selected, _ = select_context(
            symbols, scores, [changed], max_tokens=100000, top_k=5,
            graph=graph, neighbour_cap=0,
        )
        assert callee not in selected

    def test_no_graph_means_no_bypass(self):
        """Legacy callers that pass no graph keep their exact behavior."""
        changed, callee, symbols, scores, _ = _crowded_ranking()
        selected, _ = select_context(
            symbols, scores, [changed], max_tokens=100000, top_k=5,
        )
        assert callee not in selected


@pytest.mark.skipif(
    not os.path.isdir(os.path.join(REQUESTS_REPO, ".git")),
    reason="benchmark_repos/requests not present (gitignored; clone to run)",
)
def test_has_read_is_retrieved_end_to_end():
    """The canonical regression, pinned against the real pipeline.

    Before the bypass: has_read ranked 26 of 247 and the 10k-budget package
    held 16 symbols, so a direct cross-file callee was cut. If this fails,
    suspect graph extraction or neighbour identification before the cap.
    """
    from diffcontext.pipeline import index_repository, analyze_impact
    from diffcontext.pipeline import compile as dc_compile

    idx = index_repository(REQUESTS_REPO)
    changed = "./src/requests/models.py:RequestEncodingMixin._encode_files"
    target = "./src/requests/_types.py:has_read"
    assert target in idx.graph.get(changed, []), "graph lost the call edge"

    impact = analyze_impact(idx, [changed], max_depth=2)
    pkg = dc_compile(idx, impact, max_tokens=10000, top_k=20)
    assert target in {item.symbol_id for item in pkg.items}
