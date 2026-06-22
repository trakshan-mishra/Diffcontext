#!/usr/bin/env python3
"""
tests/test_core.py — Unit tests for DiffContext core algorithm.

Runs entirely against small, checked-in fixture repos under
tests/fixtures/ -- no external clone (Flask/FastAPI/Click) required, so
nothing here silently skips in a fresh checkout or CI environment.

For larger-scale, real-world precision/recall numbers (the kind only a
big real repo with git history can give you), see benchmark_runner.py
and BENCHMARKS.md instead -- that's a separate, opt-in suite, not part
of the fast unit-test path.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.parser import extract_all_symbols, extract_symbols
from diffcontext.scanner import find_python_files
from diffcontext.resolver import build_import_map
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import get_blast_radius
from diffcontext.impact.traversal import expand_dependencies
from diffcontext.impact.scoring import compute_impact_scores
from diffcontext.context.selector import select_context
from diffcontext.context.compiler import compile_context
from diffcontext.pipeline import index_repository, analyze_impact, compile


# ---- Paths ----
# Fixtures are small, hand-built repos checked into the repo itself --
# see tests/fixtures/simple_repo and tests/fixtures/medium_repo.
BASE = os.path.dirname(os.path.dirname(__file__))
FIXTURES = os.path.join(BASE, "tests", "fixtures")
SIMPLE = os.path.join(FIXTURES, "simple_repo")
MEDIUM = os.path.join(FIXTURES, "medium_repo")


def _require_fixture(path, name):
    """
    Fail loudly (not skip) if a checked-in fixture is missing -- unlike
    an external clone, there's no legitimate reason for this to be absent
    in a real checkout, so a missing fixture is a real test failure, not
    something to silently skip past.
    """
    if not os.path.isdir(path):
        pytest.fail(
            f"{name} fixture missing at {path} -- it should be checked "
            f"into the repo under tests/fixtures/. This is not an "
            f"external dependency; if it's missing, something is wrong "
            f"with the checkout."
        )


# ---- Scanner tests ----

class TestScanner:
    def test_find_python_files_simple(self):
        _require_fixture(SIMPLE, "simple_repo")
        files = find_python_files(SIMPLE)
        assert len(files) >= 1
        assert all(f.endswith(".py") for f in files)

    def test_excludes_pycache(self, tmp_path):
        # Build a tiny repo with a __pycache__ dir inline, rather than
        # depending on an external clone to have one lying around.
        (tmp_path / "main.py").write_text("def f():\n    return 1\n")
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "main.cpython-312.pyc").write_text("not real bytecode")

        files = find_python_files(str(tmp_path))
        assert not any("__pycache__" in f for f in files)


# ---- Parser tests ----

class TestParser:
    def test_extract_simple(self):
        _require_fixture(SIMPLE, "simple_repo")
        symbols = extract_all_symbols(SIMPLE)
        assert len(symbols) > 0
        # Check symbol IDs have correct format
        for sym_id in symbols:
            assert ":" in sym_id
            assert sym_id.startswith("./")

    def test_extracts_methods(self):
        _require_fixture(MEDIUM, "medium_repo")
        symbols = extract_all_symbols(MEDIUM)
        # medium_repo's models.py has class methods (User, Order)
        method_syms = [s for s in symbols if "." in s.split(":", 1)[1]]
        assert len(method_syms) > 0, "Should extract class methods"


# ---- Graph Builder tests ----

class TestGraphBuilder:
    def test_simple_graph(self):
        _require_fixture(SIMPLE, "simple_repo")
        graph = build_repository_graph(SIMPLE)
        assert len(graph) > 0

    def test_medium_graph(self):
        _require_fixture(MEDIUM, "medium_repo")
        graph = build_repository_graph(MEDIUM)
        assert len(graph) > 0
        # medium_repo's service.py imports from models.py and validators.py
        edges = sum(len(deps) for deps in graph.values())
        assert edges > 0, "Should have cross-file dependency edges"

    def test_no_self_loops(self):
        # Structural invariant: a function should never list itself as
        # its own dependency. Originally only checked against Flask;
        # medium_repo's onboard_user -> create_user -> create_order chain
        # is enough to exercise the same code path.
        _require_fixture(MEDIUM, "medium_repo")
        graph = build_repository_graph(MEDIUM)
        for sym_id, deps in graph.items():
            assert sym_id not in deps, f"Self-loop found: {sym_id}"


# ---- Blast Radius tests ----

class TestBlastRadius:
    def test_blast_radius_simple(self):
        graph = {
            "a": ["b"],
            "b": ["c"],
            "c": [],
        }
        # If c changes, b calls c, a calls b -> both affected
        affected = get_blast_radius(graph, "c")
        assert "b" in affected
        assert "a" in affected
        assert "c" not in affected  # c itself not in blast radius

    def test_blast_radius_cycle(self):
        graph = {
            "a": ["b"],
            "b": ["a"],  # cycle!
        }
        # Should not infinite loop
        affected = get_blast_radius(graph, "a")
        assert "b" in affected

    def test_blast_radius_isolated(self):
        graph = {
            "a": [],
            "b": [],
        }
        affected = get_blast_radius(graph, "a")
        assert len(affected) == 0


# ---- Traversal tests ----

class TestTraversal:
    def test_expand_full(self):
        graph = {
            "a": ["b", "c"],
            "b": ["d"],
            "c": [],
            "d": [],
        }
        result = expand_dependencies(graph, ["a"])
        assert set(result) == {"a", "b", "c", "d"}

    def test_expand_depth_1(self):
        graph = {
            "a": ["b", "c"],
            "b": ["d"],
            "c": [],
            "d": [],
        }
        result = expand_dependencies(graph, ["a"], max_depth=1)
        assert "a" in result
        assert "b" in result
        assert "c" in result
        assert "d" not in result  # 2 hops away

    def test_expand_cycle(self):
        graph = {
            "a": ["b"],
            "b": ["c"],
            "c": ["a"],
        }
        result = expand_dependencies(graph, ["a"])
        assert set(result) == {"a", "b", "c"}


# ---- Scoring tests ----

class TestScoring:
    def test_changed_gets_100(self):
        graph = {"a": ["b"], "b": []}
        scores = compute_impact_scores(graph, ["a"], {"a": []})
        assert scores["a"] >= 100

    def test_callee_gets_high_score(self):
        graph = {"a": ["b"], "b": []}
        scores = compute_impact_scores(graph, ["a"], {"a": []})
        assert scores["b"] > 50

    def test_caller_gets_high_score(self):
        graph = {"a": ["b"], "b": []}
        scores = compute_impact_scores(graph, ["b"], {"b": ["a"]})
        assert scores["a"] > 50


# ---- Pipeline integration tests ----

class TestPipeline:
    def test_full_pipeline_medium(self):
        _require_fixture(MEDIUM, "medium_repo")
        idx = index_repository(MEDIUM)
        assert len(idx.symbols) > 0
        assert len(idx.graph) > 0

        # Pick a symbol with real dependencies (onboard_user calls into
        # both create_user and create_order) so the pipeline actually
        # exercises blast radius / scoring / selection meaningfully,
        # rather than picking an arbitrary (possibly isolated) symbol.
        sym_id = "./service.py:onboard_user"
        assert sym_id in idx.graph, (
            f"Expected fixture to contain {sym_id}; fixture may have "
            f"changed without updating this test."
        )

        impact = analyze_impact(idx, [sym_id])
        assert len(impact.scores) > 0
        assert impact.scores[sym_id] >= 100

        ctx = compile(idx, impact)
        assert ctx.symbol_count > 0
        assert ctx.token_estimate > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])