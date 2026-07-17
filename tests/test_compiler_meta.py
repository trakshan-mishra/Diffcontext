"""
tests/test_compiler_meta.py — The compiler's meta-header must be derived from
the LIVE scoring constants, never hardcoded prose.

Regression context: compiler.py used to hardcode
"changed=100 | direct_callee=90 | direct_caller=80 | 2hop_callee=60 |
2hop_caller=50 | indegree*2+outdegree bonus", which silently went stale when
scoring.py's constants changed (CALLER_BASE became 85, decays became
0.65/0.85, the structural bonus became log-scaled and capped). These tests
recompute the expected values from scoring.py at test time, so any future
constant change that isn't reflected in the meta-header fails loudly.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.impact import scoring
from diffcontext.impact.scoring import describe_scoring_basis
from diffcontext.context.compiler import compile_context
from diffcontext.models import Symbol


def _make_symbols():
    """Two tiny symbols, enough to drive compile_context end to end."""
    return {
        "./a.py:f": Symbol(
            id="./a.py:f", file="/abs/a.py", name="f",
            code="def f():\n    return g()", lineno=1,
        ),
        "./a.py:g": Symbol(
            id="./a.py:g", file="/abs/a.py", name="g",
            code="def g():\n    return 1", lineno=4,
        ),
    }


class TestScoringBasisDerivation:
    def test_describe_matches_live_constants(self):
        text = describe_scoring_basis()
        assert f"changed={scoring.CHANGED_SCORE:.0f}" in text
        assert f"direct_callee={scoring.CALLEE_BASE:.0f}" in text
        assert f"direct_caller={scoring.CALLER_BASE:.0f}" in text
        assert f"2hop_callee={scoring.CALLEE_BASE * scoring.CALLEE_DECAY:.1f}" in text
        assert f"2hop_caller={scoring.CALLER_BASE * scoring.CALLER_DECAY:.1f}" in text
        assert f"capped at {scoring.STRUCT_MAX:.0f}" in text

    def test_stale_hardcoded_values_are_gone(self):
        # The exact stale claims the old hardcoded string made. If any of
        # these constants genuinely returns to the old value some day, the
        # derivation test above still guarantees correctness; this test
        # documents the specific historical bug.
        text = describe_scoring_basis()
        assert "direct_caller=80" not in text          # live value: 85
        assert "2hop_callee=60" not in text            # live value: 58.5
        assert "2hop_caller=50" not in text            # live value: 72.2
        assert "indegree*2+outdegree" not in text      # replaced by log-scaled cap

    def test_compiled_meta_header_uses_derived_basis(self):
        symbols = _make_symbols()
        graph = {"./a.py:f": ["./a.py:g"], "./a.py:g": []}
        pkg = compile_context(
            symbols=symbols,
            selected_ids=list(symbols),
            changed_ids=["./a.py:f"],
            scores={"./a.py:f": 100.0, "./a.py:g": 90.0},
            graph=graph,
        )
        assert f"Scoring basis         : {describe_scoring_basis()}" in pkg.text

    def test_meta_header_tracks_constant_changes(self, monkeypatch):
        # Simulate a future retuning of a constant: the meta-header must
        # follow automatically, with no compiler.py edit.
        monkeypatch.setattr(scoring, "CALLER_BASE", 77.0)
        assert "direct_caller=77" in describe_scoring_basis()


class TestMetaBudgetProportionality:
    """Regression: the meta-header must not dwarf the code it annotates.

    Found via stress testing on psf/black (648 symbols): `--max-tokens 500`
    produced ~2,600 tokens of output because the architecture snapshot (one
    line per repo file) and the top-15 dropped manifest were rendered with no
    awareness of the requested budget. Under tight budgets the snapshot must
    compact to a summary line and the dropped manifest must shrink — while
    the dropped COUNT stays fully disclosed.
    """

    def _many_symbols(self, n_files=40):
        syms = {}
        for i in range(n_files):
            sid = f"./mod{i}.py:fn{i}"
            syms[sid] = Symbol(
                id=sid, file=f"/abs/mod{i}.py", name=f"fn{i}",
                code=f"def fn{i}():\n    return {i}", lineno=1,
            )
        return syms

    def test_tight_budget_compacts_snapshot(self):
        syms = self._many_symbols()
        selected = ["./mod0.py:fn0"]
        dropped = [s for s in syms if s != selected[0]]
        ctx = compile_context(
            syms, selected, selected, {s: 50.0 for s in syms},
            graph={}, dropped_ids=dropped, max_tokens=500,
        )
        assert "snapshot omitted under tight budget" in ctx.text
        assert "KNOWN MODULES" not in ctx.text
        # honesty is not sacrificed: full dropped count still disclosed
        assert f"DROPPED SYMBOLS ({len(dropped)})" in ctx.text

    def test_generous_budget_keeps_full_snapshot(self):
        syms = self._many_symbols()
        selected = ["./mod0.py:fn0"]
        dropped = [s for s in syms if s != selected[0]]
        ctx = compile_context(
            syms, selected, selected, {s: 50.0 for s in syms},
            graph={}, dropped_ids=dropped, max_tokens=50000,
        )
        assert "KNOWN MODULES" in ctx.text

    def test_token_estimate_is_full_output(self):
        syms = self._many_symbols(n_files=5)
        selected = list(syms)[:2]
        ctx = compile_context(
            syms, selected, selected[:1], {s: 50.0 for s in syms},
            graph={}, max_tokens=4000,
        )
        # the reported estimate must cover the ENTIRE text the caller pays
        # for, not just the code sections
        assert ctx.token_estimate == max(1, len(ctx.text) // 4)
        assert "Output tokens (full)" in ctx.text


class TestStructuralCeilingCaveat:
    """The meta-header must always disclose that graph confidence measures
    STRUCTURAL completeness only — benchmarked cross-subsystem conceptual
    co-changes score 0% recall for every static method (EVAL_V2_REPORT.md
    failure taxonomy), so a confident '100%' line must never be readable as
    'nothing was missed'. This is a disclosure, same category as the DROPPED
    manifest: it may not be compacted away under any budget."""

    CAVEAT = "STRUCTURAL completeness only"

    def _compile_at(self, max_tokens):
        syms = _make_symbols()
        return compile_context(
            syms, list(syms), ["./a.py:f"], {s: 50.0 for s in syms},
            graph={"./a.py:f": ["./a.py:g"]}, max_tokens=max_tokens,
        )

    def test_caveat_present_at_every_budget(self):
        # 60 is tighter than the meta's own floor — the floor path must
        # still carry the disclosure; None is the unlimited path.
        for budget in (60, 300, 2000, 50000, None):
            ctx = self._compile_at(budget)
            assert self.CAVEAT in ctx.text, f"caveat missing at max_tokens={budget}"
            assert "cross-subsystem conceptual coupling" in ctx.text

    def test_caveat_adjacent_to_graph_confidence(self):
        ctx = self._compile_at(4000)
        lines = ctx.text.split("\n")
        conf_idx = next(i for i, l in enumerate(lines) if l.startswith("Graph confidence"))
        assert self.CAVEAT in lines[conf_idx + 1]
