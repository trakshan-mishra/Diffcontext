#!/usr/bin/env python3
"""
tests/test_index_scoping.py — Regression tests for the index-scope gotcha.

`diffcontext index .` excludes tests/, benchmarks/, docs/ by default. A
commit spanning an excluded dir produced 14 generic "was not found in the
index (typo, renamed, or deleted?)" warnings and omitted the changed file —
the tool looked broken when it was merely mis-scoped. These tests pin the
fix: the scoping is surfaced after `index`, and an unknown symbol whose
file lies in an excluded dir gets a specific, actionable warning that
names the dir and suggests `--include`, not the generic typo message.
"""

import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.scanner import find_python_files, first_excluded_dir
from diffcontext.pipeline import index_repository, warn_unknown_symbols


def _git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)


class TestScannerScoping:
    def test_excluded_dir_omitted_by_default(self, tmp_path):
        (tmp_path / "main.py").write_text("def f():\n    return 1\n")
        bench = tmp_path / "benchmarks"
        bench.mkdir()
        (bench / "bench.py").write_text("def g():\n    return 2\n")
        _git_repo(tmp_path)

        files = find_python_files(str(tmp_path))
        names = {os.path.basename(f) for f in files}
        assert names == {"main.py"}, "benchmarks/ excluded by default"

    def test_include_overrides_exclusion(self, tmp_path):
        (tmp_path / "main.py").write_text("def f():\n    return 1\n")
        bench = tmp_path / "benchmarks"
        bench.mkdir()
        (bench / "bench.py").write_text("def g():\n    return 2\n")
        _git_repo(tmp_path)

        files = find_python_files(str(tmp_path), include={"benchmarks"})
        names = {os.path.basename(f) for f in files}
        assert names == {"main.py", "bench.py"}, "--include benchmarks indexes it"

    def test_include_does_not_override_gitignore(self, tmp_path):
        # --include un-excludes a default exclusion, but .gitignore still
        # wins — a gitignored dir is not indexed even when named in include.
        (tmp_path / "main.py").write_text("def f():\n    return 1\n")
        (tmp_path / ".gitignore").write_text("tests/\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_x.py").write_text("def test_x():\n    assert True\n")
        _git_repo(tmp_path)

        files = find_python_files(str(tmp_path), include={"tests"})
        names = {os.path.basename(f) for f in files}
        assert "test_x.py" not in names, ".gitignore beats --include"


class TestFirstExcludedDir:
    def test_names_the_excluded_component(self):
        assert first_excluded_dir("./benchmarks/contextbench/x.py") == "benchmarks"
        assert first_excluded_dir("./tests/test_x.py") == "tests"

    def test_none_for_indexed_path(self):
        assert first_excluded_dir("./diffcontext/pipeline.py") is None

    def test_include_override(self):
        # A dir in `include` is not reported as excluded.
        assert first_excluded_dir("./tests/test_x.py", include={"tests"}) is None
        # A different excluded dir is still reported.
        assert first_excluded_dir("./benchmarks/x.py", include={"tests"}) == "benchmarks"


class TestOutsideTreeWarning:
    def test_names_excluded_dir_and_suggests_include(self, tmp_path, caplog):
        # The bug: a changed symbol in tests/ produced the generic "typo,
        # renamed, or deleted" warning. The fix names the excluded dir and
        # tells the user to re-run with --include tests.
        (tmp_path / "main.py").write_text("def f():\n    return 1\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_thing.py").write_text("def test_thing():\n    assert True\n")
        _git_repo(tmp_path)

        idx = index_repository(str(tmp_path))
        changed = ["./tests/test_thing.py:test_thing"]

        with caplog.at_level(logging.WARNING, logger="diffcontext.pipeline"):
            unknown = warn_unknown_symbols(idx, changed)

        assert changed[0] in unknown
        msgs = " ".join(r.message for r in caplog.records)
        assert "excluded from indexing by default" in msgs
        assert "--include tests" in msgs
        assert "typo, renamed" not in msgs, "generic message must not fire for excluded-dir files"

    def test_include_makes_symbol_resolvable(self, tmp_path, caplog):
        # End-to-end: --include tests indexes tests/, so the symbol is no
        # longer unknown and no warning fires.
        (tmp_path / "main.py").write_text("def f():\n    return 1\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_thing.py").write_text("def test_thing():\n    assert True\n")
        _git_repo(tmp_path)

        idx = index_repository(str(tmp_path), include={"tests"})
        assert "./tests/test_thing.py:test_thing" in idx.symbols

        with caplog.at_level(logging.WARNING, logger="diffcontext.pipeline"):
            unknown = warn_unknown_symbols(
                idx, ["./tests/test_thing.py:test_thing"])
        assert unknown == []

    def test_generic_warning_still_fires_for_real_typo(self, tmp_path, caplog):
        # A genuine typo (file is in an indexed dir, name is just wrong)
        # must still get the suggestion/generic path, not the excluded-dir
        # message — the fix must not regress the existing typo path.
        (tmp_path / "main.py").write_text("def real_name():\n    return 1\n")
        _git_repo(tmp_path)

        idx = index_repository(str(tmp_path))
        with caplog.at_level(logging.WARNING, logger="diffcontext.pipeline"):
            warn_unknown_symbols(idx, ["./main.py:typo_name"])

        msgs = " ".join(r.message for r in caplog.records)
        assert "excluded from indexing by default" not in msgs
        # Either a "did you mean" suggestion or the generic typo message.
        assert "was not found in the index" in msgs
