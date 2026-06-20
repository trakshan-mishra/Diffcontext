"""
scanner.py — Discover Python files in a repository.
"""

import os
from typing import List, Set

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


def find_python_files(root_dir: str) -> List[str]:
    """Walk repo tree, return list of .py file paths, skipping excluded dirs."""
    python_files = []

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [
            d for d in dirs
            if d not in EXCLUDED_DIRS
            and not d.endswith(".egg-info")
        ]

        for f in files:
            if f.endswith(".py"):
                python_files.append(os.path.join(root, f))

    return python_files
