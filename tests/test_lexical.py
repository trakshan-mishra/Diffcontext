#!/usr/bin/env python3
"""
tests/test_lexical.py — BM25 lexical signal and hybrid retrieval.

Covers the eval_v2-motivated additions:
  - LexicalIndex: BM25 scoring sanity (self-similarity, rare-term weighting)
  - analyze_impact(hybrid=True): lexically-similar but graph-disconnected
    symbols surface; hybrid=False restores pure graph behavior
  - select_context(top_k=...): the retrieval-budget cap
  - RepositoryIndex.update() invalidates the cached lexical index
"""

import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.lexical import LexicalIndex, get_lexical_index, tokenize
from diffcontext.models import Symbol
from diffcontext.context.selector import select_context
from diffcontext.pipeline import index_repository, analyze_impact


def _sym(sid: str, code: str) -> Symbol:
    file, name = sid.split(":")
    return Symbol(id=sid, file=file, name=name, code=code)


def _make_symbols(**defs):
    return {sid: _sym(sid, code) for sid, code in defs.items()}


class TestTokenize:
    def test_identifiers_lowercased_and_short_dropped(self):
        assert tokenize("def validate_jwt(x): return Token") == [
            "def", "validate_jwt", "return", "token",
        ]

    def test_numbers_and_symbols_ignored(self):
        assert tokenize("x = 42 + 7") == []


class TestLexicalIndex:
    def test_identical_code_scores_highest(self):
        symbols = _make_symbols(**{
            "./a.py:validate_jwt": "def validate_jwt(token): return decode_jwt_header(token)",
            "./b.py:check_jwt": "def check_jwt(token): return decode_jwt_header(token)",
            "./c.py:render_page": "def render_page(template): return template.render()",
        })
        idx = LexicalIndex(symbols)
        scores = idx.scores_for(symbols["./a.py:validate_jwt"].code)
        # The jwt sibling must outscore the unrelated renderer
        assert scores["./b.py:check_jwt"] > scores.get("./c.py:render_page", 0.0)

    def test_rare_terms_weighted_over_common(self):
        # frobnicate_quux appears in exactly 1 of 6 docs -> high idf;
        # self/data appear everywhere -> floored idf.
        symbols = _make_symbols(**{
            "./b.py:f2": "def f2(self): frobnicate_quux(self.data)",
            **{f"./x{i}.py:g{i}": f"def g{i}(self): return self.data"
               for i in range(5)},
        })
        idx = LexicalIndex(symbols)
        scores = idx.scores_for("frobnicate_quux(self.data)")
        # f2 shares the rare term; the g's share only ubiquitous ones
        assert scores["./b.py:f2"] > scores.get("./x0.py:g0", 0.0)

    def test_empty_index(self):
        idx = LexicalIndex({})
        assert idx.scores_for("anything at all") == {}

    def test_no_shared_terms_scores_empty_or_zero(self):
        symbols = _make_symbols(**{"./a.py:f": "def alpha_only(): pass"})
        idx = LexicalIndex(symbols)
        assert idx.scores_for("zzz_unknown_term_qqq") == {}


class TestTopK:
    def test_top_k_caps_non_changed_symbols(self):
        symbols = _make_symbols(**{
            f"./m.py:f{i}": f"def f{i}(): pass" for i in range(10)
        })
        scores = {f"./m.py:f{i}": float(100 - i) for i in range(10)}
        selected, dropped = select_context(
            symbols, scores, changed=["./m.py:f0"], top_k=3,
        )
        assert selected[0] == "./m.py:f0"
        assert len(selected) == 1 + 3          # changed + top_k
        assert len(dropped) == 6
        # Cap keeps the highest-scored candidates
        assert "./m.py:f1" in selected and "./m.py:f9" in dropped

    def test_top_k_none_is_unlimited(self):
        symbols = _make_symbols(**{
            f"./m.py:f{i}": f"def f{i}(): pass" for i in range(5)
        })
        scores = {f"./m.py:f{i}": float(i + 1) for i in range(5)}
        selected, _ = select_context(symbols, scores, changed=["./m.py:f0"])
        assert len(selected) == 5


class TestHybridPipeline:
    def _write_repo(self, tmp_path):
        # helpers.py: lexical twin of target, NO call edge to it
        (tmp_path / "helpers.py").write_text(textwrap.dedent("""
            def normalize_currency_amount(amount, currency_code):
                cleaned = str(amount).strip().replace(",", "")
                return round(float(cleaned), currency_decimals(currency_code))

            def currency_decimals(currency_code):
                return 0 if currency_code in ("JPY", "KRW") else 2
        """))
        # billing.py: the "changed" function
        (tmp_path / "billing.py").write_text(textwrap.dedent("""
            def charge_invoice(amount, currency_code):
                cleaned = str(amount).strip().replace(",", "")
                total = round(float(cleaned), 2)
                return submit_payment(total, currency_code)

            def submit_payment(total, currency_code):
                return {"total": total, "currency": currency_code}

            def unrelated_report():
                return "quarterly numbers"
        """))
        return str(tmp_path)

    def test_hybrid_surfaces_graph_disconnected_lexical_match(self):
        # Hand-built index so the graph is exactly what we say it is
        # (the real graph builder adds heuristic proximity edges, which
        # would make "disconnected" impossible to guarantee in a fixture).
        from diffcontext.models import RepositoryIndex
        symbols = _make_symbols(**{
            "./a.py:charge_invoice":
                "def charge_invoice(amount): return normalize_money_str(amount)",
            "./b.py:parse_money_amount":
                "def parse_money_amount(amount): return normalize_money_str(amount)",
            "./c.py:render_html":
                "def render_html(tpl): return tpl.render()",
        })
        idx = RepositoryIndex(
            symbols=symbols,
            graph={"./a.py:charge_invoice": []},   # twin is graph-unreachable
        )
        changed = ["./a.py:charge_invoice"]

        graph_only = analyze_impact(idx, changed, hybrid=False)
        hybrid = analyze_impact(idx, changed, hybrid=True)

        twin = "./b.py:parse_money_amount"
        # No graph path exists, so graph-only cannot see the twin...
        assert twin not in graph_only.scores
        # ...but the hybrid's lexical leg must surface it, above noise
        assert hybrid.scores.get(twin, 0.0) > \
               hybrid.scores.get("./c.py:render_html", 0.0)

    def test_hybrid_ranks_same_file_over_unrelated(self, tmp_path):
        repo = self._write_repo(tmp_path)
        idx = index_repository(repo)
        hybrid = analyze_impact(idx, ["./billing.py:charge_invoice"], hybrid=True)
        # submit_payment: called + same file + lexical overlap -> must
        # outrank the same-file but unrelated report function.
        assert hybrid.scores["./billing.py:submit_payment"] > \
               hybrid.scores["./billing.py:unrelated_report"]

    def test_changed_symbol_keeps_top_score(self, tmp_path):
        repo = self._write_repo(tmp_path)
        idx = index_repository(repo)
        changed = "./billing.py:charge_invoice"
        hybrid = analyze_impact(idx, [changed], hybrid=True)
        assert hybrid.scores[changed] == max(hybrid.scores.values())

    def test_update_invalidates_lexical_cache(self, tmp_path):
        repo = self._write_repo(tmp_path)
        idx = index_repository(repo)
        first = get_lexical_index(idx)
        assert get_lexical_index(idx) is first          # cached

        (tmp_path / "helpers.py").write_text(
            "def totally_new_function():\n    return 1\n"
        )
        idx.update(["helpers.py"])
        second = get_lexical_index(idx)
        assert second is not first                       # invalidated
        assert "./helpers.py:totally_new_function" in second.ids
