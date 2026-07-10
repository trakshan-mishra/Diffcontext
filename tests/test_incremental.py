"""
tests/test_incremental.py — Phase 3: parse-once pipeline, persistent graph
cache, and RepositoryIndex.update().

The load-bearing invariant, asserted repeatedly: an incrementally-updated
index must be EXACTLY equal (symbols, graph, broken_files) to a fresh
from-scratch index of the same on-disk state. If update() ever drifts from
full rebuild, these tests fail.
"""

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.pipeline import index_repository
from diffcontext.graph_builder import build_repository_graph

BASE = os.path.dirname(os.path.dirname(__file__))
MEDIUM = os.path.join(BASE, "tests", "fixtures", "medium_repo")


def _graph_as_comparable(graph):
    """Edge insertion order is not part of the contract; compare as sets."""
    return {k: set(v) for k, v in graph.items()}


def _fresh_index(repo):
    """Full rebuild with no cache influence."""
    db = os.path.join(repo, ".diffcontext_cache.db")
    for suffix in ("", "-shm", "-wal"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    return index_repository(repo)


def _assert_indexes_equal(a, b):
    assert set(a.symbols) == set(b.symbols)
    for sid in a.symbols:
        assert a.symbols[sid].code == b.symbols[sid].code
        assert a.symbols[sid].lineno == b.symbols[sid].lineno
    assert _graph_as_comparable(a.graph) == _graph_as_comparable(b.graph)
    assert sorted(a.broken_files) == sorted(b.broken_files)


@pytest.fixture
def repo(tmp_path):
    """Mutable copy of the medium fixture repo."""
    dst = str(tmp_path / "repo")
    shutil.copytree(MEDIUM, dst)
    # Never inherit a cache db from the fixture dir
    for suffix in ("", "-shm", "-wal"):
        try:
            os.remove(os.path.join(dst, ".diffcontext_cache.db" + suffix))
        except FileNotFoundError:
            pass
    return dst


class TestParseOnceEquivalence:
    def test_pipeline_graph_equals_standalone_graph(self, repo):
        # The refactored pipeline (shared ASTs) must produce the same graph
        # as build_repository_graph's own self-contained path.
        idx = _fresh_index(repo)
        standalone = build_repository_graph(repo)
        assert _graph_as_comparable(idx.graph) == _graph_as_comparable(standalone)
        assert len(idx.symbols) > 0


class TestGraphCache:
    def test_second_index_identical(self, repo):
        first = _fresh_index(repo)
        second = index_repository(repo)          # warm: graph-cache hit
        _assert_indexes_equal(first, second)

    def test_cache_invalidated_by_edit(self, repo):
        first = _fresh_index(repo)
        # Append a new function to some file
        target = None
        for root, _dirs, files in os.walk(repo):
            for f in files:
                if f.endswith(".py"):
                    target = os.path.join(root, f)
                    break
            if target:
                break
        with open(target, "a") as f:
            f.write("\n\ndef brand_new_fn_xyz():\n    return 42\n")
        second = index_repository(repo)
        rel = "./" + os.path.relpath(target, repo)
        assert f"{rel}:brand_new_fn_xyz" in second.symbols
        assert f"{rel}:brand_new_fn_xyz" not in first.symbols


class TestIncrementalUpdate:
    def _some_py_file(self, repo):
        for root, _dirs, files in os.walk(repo):
            for f in sorted(files):
                if f.endswith(".py") and "__init__" not in f:
                    return os.path.join(root, f)
        raise AssertionError("fixture has no .py files")

    def test_update_after_edit_matches_full_rebuild(self, repo):
        idx = _fresh_index(repo)
        target = self._some_py_file(repo)
        with open(target, "a") as f:
            f.write("\n\ndef incrementally_added():\n    return 1\n")

        idx.update([target])

        fresh = _fresh_index(repo)
        _assert_indexes_equal(idx, fresh)
        rel = "./" + os.path.relpath(target, repo)
        assert f"{rel}:incrementally_added" in idx.symbols

    def test_update_after_delete_matches_full_rebuild(self, repo):
        idx = _fresh_index(repo)
        target = self._some_py_file(repo)
        os.remove(target)
        idx.update([target])
        fresh = _fresh_index(repo)
        _assert_indexes_equal(idx, fresh)

    def test_update_after_new_file_matches_full_rebuild(self, repo):
        idx = _fresh_index(repo)
        target = os.path.join(repo, "newly_created.py")
        with open(target, "w") as f:
            f.write("def created_later():\n    return 9\n")
        idx.update([target])
        fresh = _fresh_index(repo)
        _assert_indexes_equal(idx, fresh)
        assert "./newly_created.py:created_later" in idx.symbols

    def test_update_after_syntax_break_matches_full_rebuild(self, repo):
        idx = _fresh_index(repo)
        target = self._some_py_file(repo)
        with open(target, "w") as f:
            f.write("def broken(:\n    nope\n")
        idx.update([target])
        fresh = _fresh_index(repo)
        rel = "./" + os.path.relpath(target, repo)
        assert rel in idx.broken_files
        _assert_indexes_equal(idx, fresh)

    def test_update_relative_path_accepted(self, repo):
        idx = _fresh_index(repo)
        target = self._some_py_file(repo)
        rel = os.path.relpath(target, repo)
        with open(target, "a") as f:
            f.write("\n\ndef via_rel_path():\n    return 2\n")
        idx.update([rel])                       # no leading "./"
        assert f"./{rel}:via_rel_path" in idx.symbols

    def test_update_on_warm_loaded_index(self, repo):
        # Index loaded via graph cache has no in-memory trees; update()
        # must lazily materialize them and still match a full rebuild.
        _fresh_index(repo)
        warm = index_repository(repo)           # graph-cache hit, trees=None
        target = self._some_py_file(repo)
        with open(target, "a") as f:
            f.write("\n\ndef after_warm_load():\n    return 3\n")
        warm.update([target])
        fresh = _fresh_index(repo)
        _assert_indexes_equal(warm, fresh)

    def test_update_requires_pipeline_index(self):
        from diffcontext.models import RepositoryIndex
        with pytest.raises(ValueError):
            RepositoryIndex().update(["x.py"])
