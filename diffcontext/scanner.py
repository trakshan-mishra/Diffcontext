"""
scanner.py — Discover source files in a repository.

Python always; other languages via the optional adapters in languages/
(each adapter contributes its extensions to discovery only when its
runtime deps are installed).
"""

import os
import subprocess
from typing import List, Optional, Set, Tuple

EXCLUDED_DIRS: Set[str] = {
    "__pycache__",
    ".git",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "venv",
    ".venv",
    "env",
    "node_modules",
    "experimental",
    "examples",
    "docs",
    "tests",
    "test",
    "benchmarks",
    "datasets",
    "dist",
    "build",
    "egg-info",
}

# Directories excluded from indexing by default. These are deliberately
# NOT retrieval candidates: tests/, benchmarks/, docs/ are tracked in git
# but rarely the "code that matters for a change", and indexing them would
# both bloat the graph and drown real blast-radius signal in test scaffolding.
#
# This is the single biggest practical gotcha: a commit that spans an
# excluded dir (e.g. benchmarks/) produces "was not found in the index"
# warnings for every changed symbol in that dir, and the changed file is
# omitted from the context entirely — the tool looks broken when it is
# merely mis-scoped. Override with `--include <dir>...` (see
# find_source_files / the CLI) to index an excluded dir; .gitignore still
# applies on top (a gitignored dir is not indexed even with --include).


def _is_excluded_dir(name: str, include: Optional[Set[str]] = None) -> bool:
    """True if directory `name` should be pruned, unless it is in `include`
    (a set of directory names to keep despite the default exclusions)."""
    if include and name in include:
        return False
    return name in EXCLUDED_DIRS or name.endswith(".egg-info")


def _excluded(rel_path: str, include: Optional[Set[str]] = None) -> bool:
    """True if any directory component of rel_path is excluded (and not
    overridden by `include`)."""
    parts = rel_path.replace(os.sep, "/").split("/")[:-1]
    return any(_is_excluded_dir(p, include) for p in parts)


def first_excluded_dir(
    rel_path: str, include: Optional[Set[str]] = None,
) -> Optional[str]:
    """Return the first directory component of `rel_path` that is excluded
    (and not overridden by `include`), else None.

    Used by warn_unknown_symbols to distinguish "your changed symbol's file
    is outside the indexed tree" (actionable: re-run with --include) from
    "typo / renamed / deleted" (a different kind of mistake)."""
    parts = rel_path.replace(os.sep, "/").split("/")[:-1]
    for p in parts:
        if _is_excluded_dir(p, include):
            return p
    return None


def _git_source_files(
    root_dir: str, extensions: "Tuple[str, ...]",
    include: Optional[Set[str]] = None,
) -> Optional[List[str]]:
    """
    Enumerate matching files via git: tracked + untracked-but-not-ignored.

    This makes indexing respect .gitignore, so vendored checkouts (e.g. a
    cloned benchmark repo) never pollute the index — a hardcoded dir list
    can't anticipate those. Returns None outside a git work tree or if git
    is unavailable, so the caller falls back to the filesystem walk.

    `include` overrides the hardcoded EXCLUDED_DIRS (e.g. {"benchmarks"}
    keeps benchmarks/ even though it is excluded by default). It does NOT
    override .gitignore — a gitignored dir is still omitted by git ls-files.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root_dir, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None

    matched = []
    for rel in out.stdout.decode("utf-8", "replace").split("\0"):
        if not rel.endswith(extensions) or _excluded(rel, include):
            continue
        full = os.path.join(root_dir, rel)
        # --cached lists tracked files even after deletion from disk
        if os.path.isfile(full):
            matched.append(full)
    return matched


def find_source_files(
    root_dir: str, extensions: "Tuple[str, ...]",
    include: Optional[Set[str]] = None,
) -> List[str]:
    """
    Return paths of files matching `extensions`: .gitignore-aware via git
    when root_dir is inside a git work tree, else a tree walk. Both paths
    skip EXCLUDED_DIRS (deliberate exclusions like tests/ and docs/ that
    are tracked in git but not useful retrieval candidates).

    `include` is a set of directory names to KEEP despite the default
    exclusions (e.g. {"benchmarks", "tests"} indexes those dirs too).
    Matching is by directory-name component anywhere in the tree, so
    `--include tests` un-excludes both top-level tests/ and any nested
    dir named tests/. .gitignore still applies: a gitignored dir is not
    indexed even when named in `include`.
    """
    git_files = _git_source_files(root_dir, extensions, include)
    if git_files is not None:
        return git_files

    matched = []
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not _is_excluded_dir(d, include)]

        for f in files:
            if f.endswith(extensions):
                matched.append(os.path.join(root, f))

    return matched


def find_python_files(
    root_dir: str, include: Optional[Set[str]] = None,
) -> List[str]:
    """Return list of .py file paths (see find_source_files)."""
    return find_source_files(root_dir, (".py",), include)
