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
