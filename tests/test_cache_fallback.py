#!/usr/bin/env python3
"""
tests/test_cache_fallback.py — Tests for the cache path cascade.

The cascade: repo_path/.diffcontext_cache.db → XDG cache → :memory:.
Ensures DiffContext works on read-only repos (Glama sandbox, CI checkouts,
containers with read-only rootfs, repos the user doesn't own).
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.cache import _resolve_cache_path
from diffcontext.pipeline import index_repository

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests", "fixtures")
SIMPLE = os.path.join(FIXTURES, "simple_repo")


class TestResolveCachePath:
    def test_explicit_cache_dir_takes_priority(self, tmp_path):
        """--cache-dir / env var overrides everything."""
        path = _resolve_cache_path("/some/repo", cache_dir=str(tmp_path))
        assert path == os.path.join(str(tmp_path), ".diffcontext_cache.db")

    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        """DIFFCONTEXT_CACHE_DIR env var is used when no explicit override."""
        monkeypatch.setenv("DIFFCONTEXT_CACHE_DIR", str(tmp_path))
        path = _resolve_cache_path("/some/repo")
        assert path == os.path.join(str(tmp_path), ".diffcontext_cache.db")

    def test_readonly_repo_falls_back_to_xdg(self, tmp_path, monkeypatch):
        """Read-only repo dir falls back to XDG cache."""
        monkeypatch.delenv("DIFFCONTEXT_CACHE_DIR", raising=False)
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))

        # Make repo dir read-only
        ro_repo = tmp_path / "ro_repo"
        ro_repo.mkdir()
        (ro_repo / "main.py").write_text("def f():\n    return 1\n")
        os.chmod(str(ro_repo), 0o555)  # read-only

        path = _resolve_cache_path(str(ro_repo))
        assert ".cache" in path or "xdg" in path
        assert path != os.path.join(str(ro_repo), ".diffcontext_cache.db")

        os.chmod(str(ro_repo), 0o755)  # cleanup

    def test_memory_fallback_when_all_disk_fails(self, monkeypatch):
        """When repo is read-only AND XDG cache is not writable, fall to :memory:."""
        monkeypatch.delenv("DIFFCONTEXT_CACHE_DIR", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", "/nonexistent/path/that/does/not/exist")

        path = _resolve_cache_path("/also/nonexistent")
        assert path == ":memory:", f"expected :memory:, got {path}"


class TestReadOnlyRepoIndexing:
    def test_readonly_repo_still_indexes(self, tmp_path):
        """The Glama sandbox / CI checkout use case: repo is read-only."""
        # Copy the fixture to a temp dir, make it read-only
        ro = tmp_path / "ro_repo"
        shutil.copytree(SIMPLE, str(ro))
        # Remove any existing cache
        cache = ro / ".diffcontext_cache.db"
        if cache.exists():
            cache.unlink()

        # Make the entire repo read-only
        for root, dirs, files in os.walk(str(ro)):
            os.chmod(root, 0o555)
            for f in files:
                os.chmod(os.path.join(root, f), 0o444)

        # This must not raise — the cascade falls back to XDG or :memory:
        idx = index_repository(str(ro))
        assert len(idx.symbols) > 0, "indexing must succeed on read-only repo"

        # Restore permissions for cleanup
        for root, dirs, files in os.walk(str(ro)):
            os.chmod(root, 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)

    def test_env_var_override_honored(self, tmp_path, monkeypatch):
        """DIFFCONTEXT_CACHE_DIR forces the cache location."""
        cache_dir = tmp_path / "custom_cache"
        monkeypatch.setenv("DIFFCONTEXT_CACHE_DIR", str(cache_dir))

        idx = index_repository(SIMPLE)
        assert len(idx.symbols) > 0
        assert cache_dir.exists(), "custom cache dir must be created"
        assert (cache_dir / ".diffcontext_cache.db").exists(), "cache db must be in the custom dir"

    def test_memory_cache_works_when_both_fail(self, tmp_path, monkeypatch):
        """When both repo and XDG are unwritable, :memory: still indexes."""
        ro = tmp_path / "ro_repo"
        shutil.copytree(SIMPLE, str(ro))
        cache = ro / ".diffcontext_cache.db"
        if cache.exists():
            cache.unlink()

        for root, dirs, files in os.walk(str(ro)):
            os.chmod(root, 0o555)
            for f in files:
                os.chmod(os.path.join(root, f), 0o444)

        monkeypatch.delenv("DIFFCONTEXT_CACHE_DIR", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", "/nonexistent")

        idx = index_repository(str(ro))
        assert len(idx.symbols) > 0, "indexing must succeed even with :memory: cache"

        for root, dirs, files in os.walk(str(ro)):
            os.chmod(root, 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)
