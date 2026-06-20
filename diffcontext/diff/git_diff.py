"""
git_diff.py — Extract changed files/symbols from git diff output.
"""

import os
import subprocess
from typing import List, Optional, Set


def get_changed_files(
    repo_path: str,
    ref: str = "HEAD~1",
    against: str = "HEAD",
) -> List[str]:
    """
    Get list of Python files changed between two git refs.

    Returns list of relative paths like ["./src/auth.py", "./api/login.py"]
    """
    repo_path = os.path.abspath(repo_path)
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", ref, against],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []

        files = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.endswith(".py"):
                files.append("./" + line)
        return files

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def get_changed_lines(
    repo_path: str,
    filepath: str,
    ref: str = "HEAD~1",
    against: str = "HEAD",
) -> Set[int]:
    """
    Get set of changed line numbers for a specific file.

    Returns set of 1-indexed line numbers that were added or modified.
    """
    repo_path = os.path.abspath(repo_path)
    try:
        result = subprocess.run(
            ["git", "diff", "-U0", ref, against, "--", filepath.lstrip("./")],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return set()

        changed_lines: Set[int] = set()
        for line in result.stdout.split("\n"):
            if line.startswith("@@"):
                # Parse @@ -old,count +new,count @@
                parts = line.split()
                for part in parts:
                    if part.startswith("+") and "," in part:
                        start_str, count_str = part[1:].split(",", 1)
                        start = int(start_str)
                        count = int(count_str)
                        for i in range(start, start + max(count, 1)):
                            changed_lines.add(i)
                    elif part.startswith("+") and part[1:].isdigit():
                        changed_lines.add(int(part[1:]))

        return changed_lines

    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return set()


def find_changed_symbols(
    repo_path: str,
    symbols: dict,
    ref: str = "HEAD~1",
    against: str = "HEAD",
) -> List[str]:
    """
    Find which symbol IDs are affected by a git diff.

    Cross-references changed lines with symbol line ranges to find
    exactly which functions/methods were modified.
    """
    changed_files = get_changed_files(repo_path, ref, against)
    if not changed_files:
        return []

    changed_symbols = []
    for sym_id, sym in symbols.items():
        sym_file = "./" + os.path.relpath(sym.file, os.path.abspath(repo_path))
        if sym_file not in changed_files:
            continue

        # Get changed lines for this file
        changed_lines = get_changed_lines(repo_path, sym_file, ref, against)
        if not changed_lines:
            continue

        # Check if any changed line falls within this symbol's range
        code_lines = sym.code.count("\n") + 1
        sym_lines = set(range(sym.lineno, sym.lineno + code_lines))

        if sym_lines & changed_lines:
            changed_symbols.append(sym_id)

    return changed_symbols
