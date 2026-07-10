"""
tests/test_git_diff.py — git diff → changed-symbol mapping, against a real
temporary git repository (no mocks; every test runs actual git).
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.diff.git_diff import (
    get_changed_files,
    get_changed_lines,
    find_changed_symbols,
)
from diffcontext.pipeline import index_repository


A_PY = '''\
def top(x):
    return helper(x) + 1


def helper(x):
    return x * 2


class Service:
    def run(self):
        return top(0)
'''


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


@pytest.fixture
def git_repo(tmp_path):
    repo = str(tmp_path)
    (tmp_path / "a.py").write_text(A_PY)
    (tmp_path / "b.py").write_text("def untouched():\n    return 0\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


class TestGetChangedFiles:
    def test_uncommitted_edit_visible_against_worktree(self, git_repo):
        path = os.path.join(git_repo, "a.py")
        with open(path, "a") as f:
            f.write("\n\ndef extra():\n    return 3\n")
        assert get_changed_files(git_repo, ref="HEAD") == ["./a.py"]

    def test_committed_only_mode_ignores_worktree(self, git_repo):
        path = os.path.join(git_repo, "a.py")
        with open(path, "a") as f:
            f.write("\n\ndef extra():\n    return 3\n")
        # against="HEAD" compares HEAD..HEAD → nothing
        assert get_changed_files(git_repo, ref="HEAD", against="HEAD") == []

    def test_non_python_files_excluded(self, git_repo):
        with open(os.path.join(git_repo, "notes.txt"), "w") as f:
            f.write("hello")
        _git(git_repo, "add", "notes.txt")
        _git(git_repo, "commit", "-qm", "txt")
        assert get_changed_files(git_repo, ref="HEAD~1") == []

    def test_not_a_repo_returns_empty(self, tmp_path):
        bare = tmp_path / "notrepo"
        bare.mkdir()
        assert get_changed_files(str(bare), ref="HEAD") == []


class TestGetChangedLines:
    def test_lines_match_actual_edit(self, git_repo):
        # helper(x) body is line 6 in A_PY; change exactly that line.
        lines = A_PY.split("\n")
        assert lines[5] == "    return x * 2"
        lines[5] = "    return x * 3"
        with open(os.path.join(git_repo, "a.py"), "w") as f:
            f.write("\n".join(lines))
        changed = get_changed_lines(git_repo, "./a.py", ref="HEAD")
        assert changed == {6}


class TestFindChangedSymbols:
    def test_edit_maps_to_exactly_one_symbol(self, git_repo):
        lines = A_PY.split("\n")
        lines[5] = "    return x * 3"          # inside helper()
        with open(os.path.join(git_repo, "a.py"), "w") as f:
            f.write("\n".join(lines))
        idx = index_repository(git_repo)
        changed = find_changed_symbols(git_repo, idx.symbols, ref="HEAD")
        assert changed == ["./a.py:helper"]

    def test_method_edit_maps_to_class_qualified_symbol(self, git_repo):
        lines = A_PY.split("\n")
        assert lines[10] == "        return top(0)"
        lines[10] = "        return top(1)"
        with open(os.path.join(git_repo, "a.py"), "w") as f:
            f.write("\n".join(lines))
        idx = index_repository(git_repo)
        changed = find_changed_symbols(git_repo, idx.symbols, ref="HEAD")
        assert changed == ["./a.py:Service.run"]

    def test_deleted_symbol_still_reported(self, git_repo):
        # Remove helper() entirely: it no longer exists in the current
        # index, but the diff logic must still surface it as changed.
        without_helper = A_PY.replace(
            "def helper(x):\n    return x * 2\n\n\n", ""
        )
        with open(os.path.join(git_repo, "a.py"), "w") as f:
            f.write(without_helper)
        idx = index_repository(git_repo)
        changed = find_changed_symbols(git_repo, idx.symbols, ref="HEAD")
        assert "./a.py:helper" in changed

    def test_broken_file_falls_back_to_prior_symbols(self, git_repo):
        # Introduce a SyntaxError; symbol-level diffing is impossible, so
        # the prior revision's symbol IDs and the raw patch are reported.
        with open(os.path.join(git_repo, "a.py"), "w") as f:
            f.write("def top(x:\n    oops\n")
        idx = index_repository(git_repo)
        broken, patches = [], {}
        changed = find_changed_symbols(
            git_repo, idx.symbols, ref="HEAD",
            broken_files=broken, broken_file_patches=patches,
            known_broken_files=["./a.py"],
        )
        assert broken == ["./a.py"]
        assert set(changed) >= {"./a.py:top", "./a.py:helper", "./a.py:Service.run"}
        assert "./a.py" in patches and "def top(x:" in patches["./a.py"]

    def test_untouched_repo_reports_nothing(self, git_repo):
        idx = index_repository(git_repo)
        assert find_changed_symbols(git_repo, idx.symbols, ref="HEAD") == []
