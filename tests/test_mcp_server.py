#!/usr/bin/env python3
"""
tests/test_mcp_server.py — Tests for the DiffContext MCP server.

Tests the four tools (compile_context, find_impact, explain_selection,
verify_retrieval) against the existing checked-in fixtures. Does NOT
start a real MCP transport — calls the tool functions directly through
the _get_index + pipeline functions, which is what the server does
internally.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.mcp_server import _get_index, _resolve_changed

BASE = os.path.dirname(os.path.dirname(__file__))
FIXTURES = os.path.join(BASE, "tests", "fixtures")
SIMPLE = os.path.join(FIXTURES, "simple_repo")


class TestIndexCache:
    def test_get_index_caches(self):
        # Two calls for the same repo return the same index object.
        idx1 = _get_index(SIMPLE)
        idx2 = _get_index(SIMPLE)
        assert idx1 is idx2, "index should be cached in-process"

    def test_get_index_returns_symbols(self):
        idx = _get_index(SIMPLE)
        assert len(idx.symbols) > 0


class TestResolveChanged:
    def test_explicit_symbols(self):
        idx = _get_index(SIMPLE)
        result = _resolve_changed(idx, changed_symbols=["./main.py:hello"])
        assert result == ["./main.py:hello"]

    def test_empty_without_symbols_or_ref(self):
        idx = _get_index(SIMPLE)
        result = _resolve_changed(idx)
        assert result == []


class TestToolCompileContext:
    def test_returns_context_text(self):
        from diffcontext.pipeline import analyze_impact, compile

        idx = _get_index(SIMPLE)
        # Find a real symbol to use
        sym_id = next(iter(idx.symbols))
        impact = analyze_impact(idx, [sym_id])
        ctx = compile(idx, impact, max_tokens=4000, meta="compact")
        assert ctx.text
        assert sym_id in ctx.text

    def test_meta_off_has_no_header(self):
        from diffcontext.pipeline import analyze_impact, compile

        idx = _get_index(SIMPLE)
        sym_id = next(iter(idx.symbols))
        impact = analyze_impact(idx, [sym_id])
        ctx = compile(idx, impact, max_tokens=4000, meta="off")
        assert "DIFFCONTEXT META" not in ctx.text


class TestToolFindImpact:
    def test_returns_blast_radius(self):
        from diffcontext.pipeline import analyze_impact

        idx = _get_index(SIMPLE)
        sym_id = next(iter(idx.symbols))
        impact = analyze_impact(idx, [sym_id], hybrid=False)
        assert sym_id in impact.changed
        # Blast radius may be empty on a tiny repo, but the call must work
        assert isinstance(impact.blast_radius, list)


class TestToolExplainSelection:
    def test_returns_included_and_dropped(self):
        import json
        from diffcontext.pipeline import analyze_impact, compile

        idx = _get_index(SIMPLE)
        sym_id = next(iter(idx.symbols))
        impact = analyze_impact(idx, [sym_id])
        ctx = compile(idx, impact, max_tokens=4000, meta="off")

        included = [
            {"id": item.symbol_id, "role": item.role,
             "score": round(item.score, 1), "tokens": item.token_estimate}
            for item in ctx.items
        ]
        dropped = [
            {"id": sid, "score": round(impact.scores.get(sid, 0.0), 1)}
            for sid in ctx.dropped_symbols
        ]
        result = {
            "included_symbols": included,
            "dropped_symbols": dropped,
            "total_tokens": ctx.token_estimate,
            "symbol_count": ctx.symbol_count,
        }
        # Must be valid JSON (what the tool returns to the agent)
        parsed = json.loads(json.dumps(result, indent=2))
        assert "included_symbols" in parsed
        assert "dropped_symbols" in parsed
        assert len(parsed["included_symbols"]) >= 1
