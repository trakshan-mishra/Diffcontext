"""
tests/test_agent_eval.py — the invariants of the agent evaluation layer that,
if they broke, would corrupt results silently rather than raise.

Each test here corresponds to a way the metrics could be quietly wrong:
scoring a null as a zero, crediting an arm for context the model never saw,
or billing one-time corpus setup as per-query latency.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from benchmarks.agent_eval import measure_retrieval, measure_task
from benchmarks.downstream import providers
from benchmarks.downstream.providers import compile_context, rank_fullrepo
from diffcontext.models import RepositoryIndex, Symbol


def _index(*specs) -> RepositoryIndex:
    """Build an index from (id, file, lineno, code) tuples."""
    symbols = {
        sid: Symbol(id=sid, file=f, name=sid.split(":")[-1], code=code, lineno=ln)
        for sid, f, ln, code in specs
    }
    return RepositoryIndex(symbols=symbols, graph={sid: [] for sid in symbols})


@pytest.fixture
def index() -> RepositoryIndex:
    return _index(
        ("b.py:beta", "b.py", 10, "def beta():\n    return alpha() + 1\n"),
        ("a.py:alpha", "a.py", 5, "def alpha():\n    return 1\n"),
        ("a.py:gamma", "a.py", 20, "def gamma():\n    return 3\n"),
        ("c.py:delta", "c.py", 1, "def delta():\n    return 4\n"),
    )


class TestLeaveOneOutScoring:
    def test_single_seed_task_is_null_not_zero(self, index):
        """A task with nothing to hold out has no answer, and must not be
        recorded as a miss. Scoring it 0.0 would drag every arm toward the
        floor in proportion to how many single-symbol commits a repo has —
        a task-set property masquerading as a retrieval result."""
        m = measure_retrieval(index, "fullrepo", None, ["a.py:alpha"], 10_000)

        assert m["loo_eligible"] is False
        assert m["context_precision"] is None
        assert m["context_recall"] is None
        assert m["loo_folds"] == 0

    def test_two_seeds_are_eligible_and_scored(self, index):
        m = measure_retrieval(index, "fullrepo", None,
                              ["a.py:alpha", "b.py:beta"], 10_000)

        assert m["loo_eligible"] is True
        assert m["loo_folds"] == 2
        # fullrepo shows everything, so the held-out symbol is always present.
        assert m["context_recall"] == 1.0
        assert m["context_precision"] > 0

    def test_gold_rank_is_null_when_never_found(self, index):
        """Averaging a sentinel over misses would let a shallow arm that finds
        one symbol at rank 1 outrank a deep arm that finds four at rank 8."""
        m = measure_retrieval(index, "none", None,
                              ["a.py:alpha", "b.py:beta"], 10_000)

        assert m["context_recall"] == 0.0
        assert m["gold_rank"] is None


class TestBudgetHonesty:
    def test_precision_scores_only_what_survived_the_budget(self, index):
        """An arm that ranks the right symbol but overflows the window before
        emitting it must get no credit: the model never saw it."""
        seeds = ["a.py:alpha", "b.py:beta"]
        generous = measure_retrieval(index, "fullrepo", None, seeds, 10_000)
        # A budget too small for any block to fit at all.
        starved = measure_retrieval(index, "fullrepo", None, seeds, 1)

        assert generous["context_recall"] == 1.0
        assert starved["context_recall"] == 0.0

    def test_truncation_is_reported(self, index):
        fits = compile_context(index, "fullrepo", ["a.py:alpha"], 10_000)
        starved = compile_context(index, "fullrepo", ["a.py:alpha"], 1)

        assert fits.truncated is False and fits.dropped_n == 0
        assert starved.truncated is True and starved.dropped_n > 0

    def test_included_matches_rendered_text(self, index):
        """`included` is what precision/recall are scored over, so it must be
        exactly what the prompt contains — not the ranked list."""
        res = compile_context(index, "fullrepo", ["a.py:alpha"], 10_000)

        for sid in res.included:
            assert f"# {sid}\n" in res.text
        assert len(res.included) == res.text.count("# ")


class TestFullRepoArm:
    def test_covers_every_non_seed_symbol(self, index):
        ranked = rank_fullrepo(index, ["a.py:alpha"])

        assert "a.py:alpha" not in ranked
        assert set(ranked) == set(index.symbols) - {"a.py:alpha"}

    def test_source_order_is_deterministic(self, index):
        """Source order, not hash order: the arm is 'the whole repo as a human
        would paste it', and a shuffle would make it a different baseline."""
        ranked = rank_fullrepo(index, [])

        assert ranked == ["a.py:alpha", "a.py:gamma", "b.py:beta", "c.py:delta"]
        assert rank_fullrepo(index, []) == ranked


class TestTokenAccounting:
    def test_full_repo_tokens_is_the_unbudgeted_render(self, index):
        """Reduction is only meaningful if numerator and denominator are
        measured the same way, headers and joins included."""
        measured = measure_task(index, None, ["a.py:alpha"], ["fullrepo"],
                                context_tokens=10_000, fullrepo_tokens=10_000)
        arm = measured["fullrepo"]

        assert arm["context_tokens"] == arm["full_repo_tokens"]
        assert arm["token_reduction_pct"] == 0.0

    def test_reduction_is_positive_when_arm_selects(self, index):
        measured = measure_task(index, None, ["a.py:alpha"],
                                ["fullrepo", "none"],
                                context_tokens=10_000, fullrepo_tokens=10_000)

        assert measured["none"]["context_tokens"] == 0
        assert measured["none"]["token_reduction_pct"] == 100.0


class TestLatencyAccounting:
    def test_corpus_build_is_split_out_of_query_latency(self, index, monkeypatch):
        """The one-time corpus pass belongs in its own column. Pooled into
        retrieval_ms it reports ~20s against a graph walk's ~26ms — a 700x
        gap that is entirely startup cost, and points the wrong way."""
        def slow_corpus(idx, seeds):
            time.sleep(0.05)
            providers._LAST_CORPUS_MS[0] = 50.0
            return [s for s in idx.symbols if s not in seeds]

        monkeypatch.setitem(providers.RANKERS, "semantic", slow_corpus)
        res = compile_context(index, "semantic", ["a.py:alpha"], 10_000)

        assert res.corpus_build_ms == 50.0
        # The corpus time is removed from the per-query number, not added to it.
        assert res.retrieval_ms < 50.0

    def test_corpus_cost_does_not_leak_into_the_next_arm(self, index, monkeypatch):
        """A stale module-level timer is the obvious way this breaks: arm order
        would then decide which arm looks slow."""
        def sets_corpus(idx, seeds):
            providers._LAST_CORPUS_MS[0] = 999.0
            return []

        monkeypatch.setitem(providers.RANKERS, "semantic", sets_corpus)
        compile_context(index, "semantic", ["a.py:alpha"], 10_000)
        after = compile_context(index, "bm25", ["a.py:alpha"], 10_000)

        assert after.corpus_build_ms == 0.0
