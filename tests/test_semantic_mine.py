"""Schema + provenance + determinism guarantees for the semantic pair miner.

Builds a throwaway git repo with a genuine co-change commit (two functions
modified together) and asserts the miner emits well-formed pairs carrying the
temporal-split key that keeps the Item-5 fusion feature out of the ground
truth. No network, no benchmark_repos dependency.
"""
import subprocess
import sys
from pathlib import Path

import pytest

# mine_pairs imports eval_v2_hardened -> baselines -> rank_bm25, all
# benchmark-only dependencies (requirements-benchmark.txt). Skip rather than
# fail collection on the test matrix, which installs only .[dev].
pytest.importorskip("rank_bm25", reason="benchmark deps not installed")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.semantic.mine_pairs import MinedPair, build_repo_pairs


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=repo, check=True, capture_output=True, text=True)


def _make_repo(root: Path) -> Path:
    repo = root / "toy"
    repo.mkdir()
    _git(repo, "init", "-q")
    mod = repo / "mod.py"
    mod.write_text("def alpha():\n    return beta() + 1\n\n\ndef beta():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add mod")
    # co-change commit: BOTH functions modified in one commit
    mod.write_text("def alpha():\n    return beta() + 10\n\n\ndef beta():\n    return 20\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tweak alpha and beta together")
    return repo


def test_mines_cochange_pair_with_provenance(tmp_path):
    repo = _make_repo(tmp_path)
    pairs, prov = build_repo_pairs(str(repo), target_commits=50, queries_per_commit=2)

    assert pairs, "expected at least one co-change pair"
    for p in pairs:
        assert isinstance(p, MinedPair)
        assert len(p.commit) == 40                  # full SHA, not the 8-char short form
        assert p.commit_ts > 0                       # temporal-split key present
        assert p.query_symbol not in p.gt_symbols    # a query never labels itself
        assert p.gt_symbols                          # at least one label
        assert p.query_alive_at_head is True         # both funcs survive to HEAD

    # alpha<->beta co-change must surface in BOTH query directions (round-robin)
    queries = {p.query_symbol for p in pairs}
    assert "./mod.py:alpha" in queries
    assert "./mod.py:beta" in queries
    assert prov["n_pairs"] == len(pairs)


def test_deterministic(tmp_path):
    repo = _make_repo(tmp_path)
    a, _ = build_repo_pairs(str(repo), target_commits=50, queries_per_commit=2)
    b, _ = build_repo_pairs(str(repo), target_commits=50, queries_per_commit=2)
    assert [p.query_symbol for p in a] == [p.query_symbol for p in b]
    assert [p.gt_symbols for p in a] == [p.gt_symbols for p in b]
