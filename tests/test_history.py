"""
Tests for the git co-change history signal (diffcontext/history.py) and
the adaptive hybrid blend (pipeline._adaptive_weights).
"""

import os
import subprocess

import pytest

from diffcontext.history import CoChangeIndex
from diffcontext.pipeline import (
    HYBRID_WEIGHTS, _adaptive_weights, index_repository, analyze_impact,
)


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


@pytest.fixture
def cochange_repo(tmp_path):
    """A git repo where a.py and b.py co-change twice, c.py changes alone.
    a.py and b.py share no calls, no imports, and no common vocabulary —
    only history links them (the cross-subsystem shape)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("def alpha_settings_flag():\n    return 1\n")
    (repo / "b.py").write_text("def beta_security_check():\n    return 2\n")
    (repo / "c.py").write_text("def gamma_unrelated():\n    return 3\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")

    for i in range(2):
        (repo / "a.py").write_text(
            f"def alpha_settings_flag():\n    return {10 + i}\n")
        (repo / "b.py").write_text(
            f"def beta_security_check():\n    return {20 + i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", f"co-change {i}")

    (repo / "c.py").write_text("def gamma_unrelated():\n    return 30\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "solo change")
    return str(repo)


class TestCoChangeIndex:
    def test_association_found(self, cochange_repo):
        cci = CoChangeIndex(cochange_repo, min_cochanges=2)
        scores = cci.scores_for_files(["./a.py"])
        assert "./b.py" in scores
        assert scores["./b.py"] > 0.5   # b co-changed in 2 of a's 3 commits
        assert "./c.py" not in scores   # never co-changed with a

    def test_changed_file_excluded_from_result(self, cochange_repo):
        cci = CoChangeIndex(cochange_repo, min_cochanges=1)
        scores = cci.scores_for_files(["./a.py", "./b.py"])
        assert "./a.py" not in scores and "./b.py" not in scores

    def test_min_cochanges_threshold(self, cochange_repo):
        # initial commit is the ONLY commit where c co-changed with a/b
        cci1 = CoChangeIndex(cochange_repo, min_cochanges=1)
        cci2 = CoChangeIndex(cochange_repo, min_cochanges=2)
        assert "./c.py" in cci1.scores_for_files(["./a.py"])
        assert "./c.py" not in cci2.scores_for_files(["./a.py"])

    def test_exclude_commits_removes_their_evidence(self, cochange_repo):
        log = subprocess.run(
            ["git", "log", "--format=%H"], cwd=cochange_repo,
            capture_output=True, text=True,
        ).stdout.split()
        # exclude everything: no evidence left at any threshold
        cci = CoChangeIndex(
            cochange_repo, min_cochanges=1, exclude_commits=set(log)
        )
        assert cci.scores_for_files(["./a.py"]) == {}

    def test_non_git_directory_degrades_gracefully(self, tmp_path):
        (tmp_path / "x.py").write_text("def f():\n    return 1\n")
        cci = CoChangeIndex(str(tmp_path))
        assert cci.scores_for_files(["./x.py"]) == {}

    def test_scores_for_symbols_uses_file_part(self, cochange_repo):
        cci = CoChangeIndex(cochange_repo, min_cochanges=2)
        scores = cci.scores_for_symbols(["./a.py:alpha_settings_flag"])
        assert "./b.py" in scores


class TestAdaptiveBlend:
    def test_saturated_graph_keeps_benchmarked_weights(self):
        assert _adaptive_weights(8) == HYBRID_WEIGHTS
        assert _adaptive_weights(50) == HYBRID_WEIGHTS

    def test_empty_graph_moves_all_graph_weight_to_bm25(self):
        w_g, w_b, w_f = _adaptive_weights(0)
        assert w_g == 0.0
        assert w_b == pytest.approx(HYBRID_WEIGHTS[0] + HYBRID_WEIGHTS[1])
        assert w_f == HYBRID_WEIGHTS[2]

    def test_total_weight_preserved(self):
        for n in range(0, 12):
            assert sum(_adaptive_weights(n)) == pytest.approx(sum(HYBRID_WEIGHTS))


class TestHistoryInPipeline:
    def test_history_signal_surfaces_cross_subsystem_partner(self, cochange_repo):
        idx = index_repository(cochange_repo)
        cci = CoChangeIndex(cochange_repo, min_cochanges=2)
        without = analyze_impact(idx, ["./a.py:alpha_settings_flag"])
        with_hist = analyze_impact(
            idx, ["./a.py:alpha_settings_flag"], history=cci
        )
        target = "./b.py:beta_security_check"
        assert with_hist.scores.get(target, 0.0) > without.scores.get(target, 0.0)
