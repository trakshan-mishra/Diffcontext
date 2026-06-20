#!/usr/bin/env python3
"""
ground_truth.py — Extract REAL ground truth from git co-change history.

Strategy:
    For a given commit, the ground truth is:
    "Which OTHER functions were modified in the SAME commit?"

    If a developer changed function A and function B together,
    that's evidence they are related. This is external ground truth —
    it comes from human behavior, not from our graph.

    We then test: given function A as "changed", does DiffContext
    retrieve function B?

This is the ONLY honest way to evaluate a context retrieval system.
"""

import ast
import os
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class CoChangeCase:
    """A single ground truth test case from git history."""
    commit_hash: str
    commit_msg: str
    changed_files: List[str]          # relative paths
    changed_symbols: List[str]        # function IDs that were modified
    # For testing: pick one symbol as "query", rest are ground truth
    query_symbol: str = ""
    ground_truth_symbols: List[str] = field(default_factory=list)


def _get_commits_with_multi_file_changes(
    repo_path: str,
    max_commits: int = 200,
    min_files: int = 2,
    max_files: int = 15,
) -> List[Tuple[str, str]]:
    """
    Find commits that touched multiple Python source files.
    These are interesting because they reveal real co-change relationships.

    Returns list of (commit_hash, commit_message).
    """
    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--max-count={max_commits}",
                "--format=%H|%s",
                "--no-merges",        # skip merge commits
                "--diff-filter=M",    # only modified files (not added/deleted)
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []

        commits = []
        for line in result.stdout.strip().split("\n"):
            if "|" not in line:
                continue
            hash_, msg = line.split("|", 1)

            # Check how many Python source files were changed
            files_result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=M", f"{hash_}~1", hash_],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if files_result.returncode != 0:
                continue

            py_files = [
                f for f in files_result.stdout.strip().split("\n")
                if f.endswith(".py")
                and "/test" not in f.lower()
                and "/tests/" not in f.lower()
                and "test_" not in os.path.basename(f)
                and f.strip()
            ]

            if min_files <= len(py_files) <= max_files:
                commits.append((hash_, msg))

        return commits

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def _get_changed_line_ranges(
    repo_path: str,
    commit_hash: str,
    filepath: str,
) -> Set[int]:
    """Get line numbers that were modified in a specific commit for a file."""
    try:
        result = subprocess.run(
            ["git", "diff", "-U0", f"{commit_hash}~1", commit_hash, "--", filepath],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return set()

        changed_lines: Set[int] = set()
        for line in result.stdout.split("\n"):
            if line.startswith("@@"):
                parts = line.split()
                for part in parts:
                    if part.startswith("+") and not part.startswith("+++"):
                        try:
                            if "," in part:
                                start_str, count_str = part[1:].split(",", 1)
                                start = int(start_str)
                                count = int(count_str)
                                for i in range(start, start + max(count, 1)):
                                    changed_lines.add(i)
                            else:
                                val = part[1:]
                                if val.isdigit():
                                    changed_lines.add(int(val))
                        except ValueError:
                            continue

        return changed_lines

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()


def _find_functions_at_lines(
    filepath: str,
    changed_lines: Set[int],
    repo_path: str,
) -> List[str]:
    """
    Find which function IDs contain the changed lines.
    Uses AST parsing at the commit's version of the file.
    """
    try:
        abs_path = os.path.join(repo_path, filepath)
        if not os.path.isfile(abs_path):
            return []

        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()

        tree = ast.parse(source)
    except (SyntaxError, FileNotFoundError, OSError):
        return []

    relative_file = "./" + filepath
    results = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Find parent class if any
            class_name = _find_parent_class(tree, node)
            if class_name:
                name = f"{class_name}.{node.name}"
            else:
                name = node.name

            func_id = f"{relative_file}:{name}"

            # Check if any changed line falls within this function
            end_lineno = getattr(node, "end_lineno", node.lineno + 10)
            func_lines = set(range(node.lineno, end_lineno + 1))

            if func_lines & changed_lines:
                results.append(func_id)

    return results


def _find_parent_class(tree, target_node):
    """Find the class that contains a function node."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if child is target_node:
                    return node.name
                # Check nested
                if isinstance(child, ast.ClassDef):
                    for grandchild in child.body:
                        if grandchild is target_node:
                            return f"{node.name}.{child.name}"
    return None


def extract_cochange_cases(
    repo_path: str,
    max_cases: int = 30,
    min_symbols_per_case: int = 2,
) -> List[CoChangeCase]:
    """
    Extract real co-change test cases from git history.

    Each case: one commit where multiple source functions were changed.
    Query = one of those functions.
    Ground truth = the other functions changed in the same commit.
    """
    repo_path = os.path.abspath(repo_path)

    commits = _get_commits_with_multi_file_changes(repo_path)
    if not commits:
        return []

    cases = []

    for commit_hash, commit_msg in commits:
        if len(cases) >= max_cases:
            break

        # Get changed source files
        files_result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=M", f"{commit_hash}~1", commit_hash],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if files_result.returncode != 0:
            continue

        py_files = [
            f for f in files_result.stdout.strip().split("\n")
            if f.endswith(".py")
            and "/test" not in f.lower()
            and "/tests/" not in f.lower()
            and "test_" not in os.path.basename(f)
            and f.strip()
        ]

        if len(py_files) < 2:
            continue

        # Find which functions were changed in each file
        all_changed_symbols = []
        for filepath in py_files:
            changed_lines = _get_changed_line_ranges(repo_path, commit_hash, filepath)
            if not changed_lines:
                continue

            symbols = _find_functions_at_lines(filepath, changed_lines, repo_path)
            all_changed_symbols.extend(symbols)

        if len(all_changed_symbols) < min_symbols_per_case:
            continue

        # Create test case: first symbol = query, rest = ground truth
        case = CoChangeCase(
            commit_hash=commit_hash[:8],
            commit_msg=commit_msg[:80],
            changed_files=["./" + f for f in py_files],
            changed_symbols=all_changed_symbols,
            query_symbol=all_changed_symbols[0],
            ground_truth_symbols=all_changed_symbols[1:],
        )
        cases.append(case)

    return cases


if __name__ == "__main__":
    import sys
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    cases = extract_cochange_cases(repo)
    print(f"Found {len(cases)} co-change test cases")
    for case in cases[:5]:
        print(f"\n  Commit: {case.commit_hash} — {case.commit_msg}")
        print(f"  Query: {case.query_symbol}")
        print(f"  Ground truth ({len(case.ground_truth_symbols)}):")
        for gt in case.ground_truth_symbols:
            print(f"    {gt}")
