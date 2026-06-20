#!/usr/bin/env python3
"""
tests/test_core.py — Unit tests for DiffContext core algorithm.

Tests against the small repos in benchmarks/datasets/ and against
the real Flask/FastAPI/Click repos.
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
BASE = os.path.dirname(os.path.dirname(__file__))
DATASETS = os.path.join(BASE, "benchmarks", "datasets")
SIMPLE = os.path.join(DATASETS, "simple_repo")
MEDIUM = os.path.join(DATASETS, "medium_repo")
TORTURE = os.path.join(DATASETS, "torture_repo")
FLASK = os.path.join(BASE, "benchmarks", "flask")
FASTAPI = os.path.join(BASE, "benchmarks", "fastapi")
CLICK = os.path.join(BASE, "benchmarks", "click")


# ---- Scanner tests ----

class TestScanner:
    def test_find_python_files_simple(self):
        if not os.path.isdir(SIMPLE):
            pytest.skip("simple_repo not found")
        files = find_python_files(SIMPLE)
        assert len(files) >= 1
        assert all(f.endswith(".py") for f in files)

    def test_excludes_pycache(self):
        if not os.path.isdir(FLASK):
            pytest.skip("Flask repo not found")
        files = find_python_files(FLASK)
        assert not any("__pycache__" in f for f in files)


# ---- Parser tests ----

class TestParser:
    def test_extract_simple(self):
        if not os.path.isdir(SIMPLE):
            pytest.skip("simple_repo not found")
        symbols = extract_all_symbols(SIMPLE)
        assert len(symbols) > 0
        # Check symbol IDs have correct format
        for sym_id in symbols:
            assert ":" in sym_id
            assert sym_id.startswith("./")

    def test_extracts_methods(self):
        if not os.path.isdir(FLASK):
            pytest.skip("Flask repo not found")
        symbols = extract_all_symbols(FLASK)
        # Flask has class methods
        method_syms = [s for s in symbols if "." in s.split(":", 1)[1]]
        assert len(method_syms) > 0, "Should extract class methods"


# ---- Graph Builder tests ----

class TestGraphBuilder:
    def test_simple_graph(self):
        if not os.path.isdir(SIMPLE):
            pytest.skip("simple_repo not found")
        graph = build_repository_graph(SIMPLE)
        assert len(graph) > 0

    def test_medium_graph(self):
        if not os.path.isdir(MEDIUM):
            pytest.skip("medium_repo not found")
        graph = build_repository_graph(MEDIUM)
        assert len(graph) > 0
        # Medium repo should have cross-file deps
        edges = sum(len(deps) for deps in graph.values())
        assert edges > 0, "Should have cross-file dependency edges"

    def test_flask_graph(self):
        if not os.path.isdir(FLASK):
            pytest.skip("Flask repo not found")
        graph = build_repository_graph(FLASK)
        assert len(graph) > 50, f"Flask should have many symbols, got {len(graph)}"
        edges = sum(len(deps) for deps in graph.values())
        assert edges > 20, f"Flask should have many edges, got {edges}"

    def test_no_self_loops(self):
        if not os.path.isdir(FLASK):
            pytest.skip("Flask repo not found")
        graph = build_repository_graph(FLASK)
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
        if not os.path.isdir(MEDIUM):
            pytest.skip("medium_repo not found")
        idx = index_repository(MEDIUM)
        assert len(idx.symbols) > 0
        assert len(idx.graph) > 0

        # Pick first symbol
        sym_id = list(idx.graph.keys())[0]
        impact = analyze_impact(idx, [sym_id])
        assert len(impact.scores) > 0

        ctx = compile(idx, impact)
        assert ctx.symbol_count > 0
        assert ctx.token_estimate > 0

    def test_flask_pipeline(self):
        if not os.path.isdir(FLASK):
            pytest.skip("Flask repo not found")
        idx = index_repository(FLASK)
        assert len(idx.symbols) > 50

        # Pick a connected symbol
        best_sym = None
        best_deps = 0
        for sym_id, deps in idx.graph.items():
            if len(deps) > best_deps:
                best_deps = len(deps)
                best_sym = sym_id

        if best_sym:
            impact = analyze_impact(idx, [best_sym])
            ctx = compile(idx, impact)
            assert ctx.reduction_pct > 0, "Should reduce tokens vs full repo"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
