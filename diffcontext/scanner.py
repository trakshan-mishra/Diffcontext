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


def _excluded(rel_path: str) -> bool:
    """True if any directory component of rel_path is on the exclusion list."""
    parts = rel_path.replace(os.sep, "/").split("/")[:-1]
    return any(p in EXCLUDED_DIRS or p.endswith(".egg-info") for p in parts)


def _git_source_files(
    root_dir: str, extensions: "Tuple[str, ...]"
) -> Optional[List[str]]:
    """
    Enumerate matching files via git: tracked + untracked-but-not-ignored.

    This makes indexing respect .gitignore, so vendored checkouts (e.g. a
    cloned benchmark repo) never pollute the index — a hardcoded dir list
    can't anticipate those. Returns None outside a git work tree or if git
    is unavailable, so the caller falls back to the filesystem walk.
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
        if not rel.endswith(extensions) or _excluded(rel):
            continue
        full = os.path.join(root_dir, rel)
        # --cached lists tracked files even after deletion from disk
        if os.path.isfile(full):
            matched.append(full)
    return matched


def find_source_files(
    root_dir: str, extensions: "Tuple[str, ...]"
) -> List[str]:
    """
    Return paths of files matching `extensions`: .gitignore-aware via git
    when root_dir is inside a git work tree, else a tree walk. Both paths
    skip EXCLUDED_DIRS (deliberate exclusions like tests/ and docs/ that
    are tracked in git but not useful retrieval candidates).
    """
    git_files = _git_source_files(root_dir, extensions)
    if git_files is not None:
        return git_files

    matched = []
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [
            d for d in dirs
            if d not in EXCLUDED_DIRS
            and not d.endswith(".egg-info")
        ]

        for f in files:
            if f.endswith(extensions):
                matched.append(os.path.join(root, f))

    return matched


def find_python_files(root_dir: str) -> List[str]:
    """Return list of .py file paths (see find_source_files)."""
    return find_source_files(root_dir, (".py",))
