"""
tests/test_harness_api.py — Phase 4 harness-facing surface: structured
ContextItem output, pluggable token counter, ScoringConfig, session-scoped
warning state, and cache thread-safety.
"""

import logging
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.impact.scoring import (
    ScoringConfig, compute_impact_scores, describe_scoring_basis,
)
from diffcontext.context.selector import select_context
from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile
from diffcontext.cache import SymbolCache
from diffcontext.models import Symbol

BASE = os.path.dirname(os.path.dirname(__file__))
MEDIUM = os.path.join(BASE, "tests", "fixtures", "medium_repo")


class TestScoringConfig:
    def test_defaults_match_module_constants(self):
        from diffcontext.impact import scoring
        cfg = ScoringConfig()
        assert cfg.caller_base == scoring.CALLER_BASE
        assert cfg.callee_decay == scoring.CALLEE_DECAY
        assert cfg.struct_max == scoring.STRUCT_MAX

    def test_custom_config_changes_scores(self):
        graph = {"a": ["b"], "b": [], "c": ["a"]}
        # caller_base dominates c's score; halving it must halve c
        hi = compute_impact_scores(graph, ["a"], {}, config=ScoringConfig(caller_base=85.0, struct_max=0.0, sibling_base=0.0))
        lo = compute_impact_scores(graph, ["a"], {}, config=ScoringConfig(caller_base=42.5, struct_max=0.0, sibling_base=0.0))
        assert hi["c"] == pytest.approx(85.0)
        assert lo["c"] == pytest.approx(42.5)

    def test_describe_reflects_custom_config(self):
        text = describe_scoring_basis(ScoringConfig(caller_base=77.0))
        assert "direct_caller=77" in text

    def test_pipeline_accepts_config(self):
        idx = index_repository(MEDIUM)
        changed = [next(iter(idx.symbols))]
        default = analyze_impact(idx, changed)
        boosted = analyze_impact(
            idx, changed, scoring_config=ScoringConfig(changed_score=500.0)
        )
        assert boosted.scores[changed[0]] > default.scores[changed[0]]


class TestPluggableTokenizer:
    def _symbols(self, n=5, size=400):
        return {
            f"./m.py:f{i}": Symbol(
                id=f"./m.py:f{i}", file="/abs/m.py", name=f"f{i}",
                code="x" * size, lineno=i * 10,
            )
            for i in range(n)
        }

    def test_custom_counter_is_used_for_budget(self):
        symbols = self._symbols()
        scores = {sid: 50.0 for sid in symbols}
        calls = []

        def counter(text):
            calls.append(text)
            return 1000  # every symbol claims 1000 tokens

        selected, dropped = select_context(
            symbols, scores, changed=[], max_tokens=2500, token_counter=counter,
        )
        assert calls, "custom counter was never invoked"
        # per-symbol cap = 25% of 2500 = 625 < 1000 → everything dropped
        assert selected == [] and len(dropped) == 5

        # Same corpus under the default heuristic (~120 tokens each) fits all
        selected2, dropped2 = select_context(symbols, scores, changed=[], max_tokens=2500)
        assert len(selected2) == 5 and dropped2 == []

    def test_pipeline_token_counter_reaches_package(self):
        idx = index_repository(MEDIUM)
        changed = [next(iter(idx.symbols))]
        impact = analyze_impact(idx, changed)
        pkg = dc_compile(idx, impact, max_tokens=10000,
                         token_counter=lambda t: len(t))  # 1 token per char
        heuristic = dc_compile(idx, impact, max_tokens=10000)
        # counting chars gives ~4x the heuristic's estimate
        assert pkg.token_estimate > heuristic.token_estimate * 2


class TestStructuredItems:
    def test_items_present_and_consistent_with_text(self):
        idx = index_repository(MEDIUM)
        changed = [next(iter(idx.symbols))]
        impact = analyze_impact(idx, changed)
        pkg = dc_compile(idx, impact, max_tokens=10000)

        assert pkg.items, "no structured items produced"
        assert len(pkg.items) == pkg.symbol_count
        roles = {i.role for i in pkg.items}
        assert roles <= {"changed", "impacted", "dependency"}

        by_id = {i.symbol_id: i for i in pkg.items}
        assert by_id[changed[0]].role == "changed"

        for item in pkg.items:
            assert item.code in pkg.text          # renderer built on items
            assert item.token_estimate > 0
            assert item.symbol_id in idx.symbols

    def test_relationships_come_from_graph(self):
        idx = index_repository(MEDIUM)
        changed = [next(iter(idx.symbols))]
        impact = analyze_impact(idx, changed)
        pkg = dc_compile(idx, impact, max_tokens=10000)
        for item in pkg.items:
            assert item.callees == list(idx.graph.get(item.symbol_id, []))
            for caller in item.callers:
                assert item.symbol_id in idx.graph.get(caller, [])


class TestSessionScopedWarnings:
    def test_reindex_warns_again_in_same_process(self, tmp_path, caplog):
        # Module-global dedup used to suppress the second session's warning
        # entirely in a long-lived process. Per-session state must warn once
        # per indexing session.
        (tmp_path / "bad.py").write_text("def broken(:\n")
        with caplog.at_level(logging.WARNING, logger="diffcontext.pipeline"):
            index_repository(str(tmp_path))
            first = sum("SyntaxError" in r.message for r in caplog.records)
            index_repository(str(tmp_path))
            second = sum("SyntaxError" in r.message for r in caplog.records)
        assert first == 1
        assert second == 2, "second indexing session's warning was suppressed"


class TestCacheThreadSafety:
    def test_concurrent_get_or_parse(self, tmp_path):
        files = []
        for i in range(8):
            f = tmp_path / f"m{i}.py"
            f.write_text(f"def fn{i}():\n    return {i}\n")
            files.append(str(f))

        def parse(path):
            name = "fn" + os.path.basename(path)[1:-3]
            return {f"./{os.path.basename(path)}:{name}": Symbol(
                id=f"./{os.path.basename(path)}:{name}", file=path,
                name=name, code="def ...", lineno=1,
            )}

        errors = []
        with SymbolCache(str(tmp_path / "cache.db")) as cache:
            def worker():
                try:
                    for f in files:
                        result = cache.get_or_parse(f, parse)
                        assert len(result) == 1
                except Exception as e:      # noqa: BLE001 — recorded for assert
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert errors == [], f"concurrent cache access failed: {errors}"
