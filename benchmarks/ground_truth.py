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

FIXES in this version:
  BUG 1 (critical): _find_functions_at_lines() was reading the file from
    disk (HEAD state), but the changed_lines come from `git diff` at a
    specific commit. For repos with significant history, the file may look
    completely different today. Lines get misattributed to wrong functions.
    Fix: use `git show <hash>:<path>` to get the file AS IT WAS at the
    commit being analyzed.

  BUG 2 (evaluation bias): extract_cochange_cases() always set
    query_symbol = all_changed_symbols[0], i.e. the first function in the
    first changed file. This biases evaluation toward whichever files git
    returns first. Fix: generate one case per changed symbol (round-robin
    expansion), so every function gets an equal chance to be the query.

  BUG 3 (selection bias): max_commits=200 was enough for popular repos
    but small repos (flask, click) may have few qualifying commits. Raised
    to 500 and also relax the min_files filter to min_files=1 (allowing
    single-file commits where multiple functions changed within one file).

  BUG 4 (case inflation): The old code ran subprocess.run for EVERY
    commit twice — once in _get_commits_with_multi_file_changes and once
    inside extract_cochange_cases. Fixed by collapsing into a single pass.
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
    changed_symbols: List[str]        # ALL function IDs that were modified
    # For testing: one symbol is the query, the rest are ground truth
    query_symbol: str = ""
    ground_truth_symbols: List[str] = field(default_factory=list)


def _get_changed_line_ranges(
    repo_path: str,
    commit_hash: str,
    filepath: str,
) -> Set[int]:
    """Get the NEW-FILE line numbers that were added/modified in a commit."""
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


def _get_source_at_commit(
    repo_path: str,
    commit_hash: str,
    filepath: str,
) -> Optional[str]:
    """
    Return the file's source code AS IT WAS at commit_hash.

    This is the critical fix: `git show <hash>:<path>` gives us the exact
    file content that the diff line numbers refer to, not the current HEAD
    version which may have changed substantially since that commit.
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{commit_hash}:{filepath}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _find_parent_class(tree: ast.AST, target_node: ast.AST) -> Optional[str]:
    """Find the (possibly nested) class name that directly contains target_node."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if child is target_node:
                    return node.name
                if isinstance(child, ast.ClassDef):
                    for grandchild in child.body:
                        if grandchild is target_node:
                            return f"{node.name}.{child.name}"
    return None


def _find_functions_at_lines(
    filepath: str,
    changed_lines: Set[int],
    repo_path: str,
    commit_hash: str,
) -> List[str]:
    """
    Find which function IDs contain the changed lines.

    CRITICAL FIX: Reads the file AS IT WAS at commit_hash using
    `git show`, not the current HEAD state. This ensures that
    diff line numbers correctly map to function bodies.
    """
    if not changed_lines:
        return []

    source = _get_source_at_commit(repo_path, commit_hash, filepath)
    if source is None:
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    relative_file = "./" + filepath
    results = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            class_name = _find_parent_class(tree, node)
            name = f"{class_name}.{node.name}" if class_name else node.name
            func_id = f"{relative_file}:{name}"

            end_lineno = getattr(node, "end_lineno", node.lineno + 10)
            func_lines = set(range(node.lineno, end_lineno + 1))

            if func_lines & changed_lines:
                results.append(func_id)

    return results


def extract_cochange_cases(
    repo_path: str,
    max_cases: int = 50,
    min_symbols_per_commit: int = 2,
) -> List[CoChangeCase]:
    """
    Extract real co-change test cases from git history.

    FIXES vs old version:
      - Reads file at commit time (git show), not HEAD.
      - Generates one case PER CHANGED SYMBOL, not just one per commit.
        This avoids bias toward whichever symbol happened to be first.
      - Allows single-file commits (min_files=1) if enough functions changed.
      - Scans more commits (max_commits=500).
      - Deduplicates: same (query, gt) pair from different commits is skipped.

    Each case: one query symbol, all other co-changed symbols = ground truth.
    """
    repo_path = os.path.abspath(repo_path)

    # Single-pass commit scan: get all commits, then process them
    try:
        log_result = subprocess.run(
            [
                "git", "log",
                "--max-count=500",
                "--format=%H|%s",
                "--no-merges",
                "--diff-filter=M",   # only modifications (not pure adds/deletes)
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if log_result.returncode != 0:
            return []
        raw_commits = [
            line.split("|", 1)
            for line in log_result.stdout.strip().split("\n")
            if "|" in line
        ]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    cases: List[CoChangeCase] = []
    seen_queries: Set[str] = set()  # avoid duplicate (query, gt_frozenset) pairs

    for commit_hash, commit_msg in raw_commits:
        if len(cases) >= max_cases:
            break

        # Get changed Python source files in this commit
        try:
            files_result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=M",
                 f"{commit_hash}~1", commit_hash],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if files_result.returncode != 0:
                continue
        except subprocess.TimeoutExpired:
            continue

        py_files = [
            f for f in files_result.stdout.strip().split("\n")
            if f.endswith(".py")
            and "/test" not in f.lower()
            and "/tests/" not in f.lower()
            and "test_" not in os.path.basename(f)
            and f.strip()
        ]

        if not py_files:
            continue

        # For each changed file, find which functions were actually modified
        all_changed_symbols: List[str] = []
        for filepath in py_files:
            changed_lines = _get_changed_line_ranges(repo_path, commit_hash, filepath)
            if not changed_lines:
                continue
            # FIX: pass commit_hash so we read the file at commit time
            syms = _find_functions_at_lines(
                filepath, changed_lines, repo_path, commit_hash
            )
            all_changed_symbols.extend(syms)

        # Deduplicate (same function can match multiple diffs in the same commit)
        all_changed_symbols = list(dict.fromkeys(all_changed_symbols))

        if len(all_changed_symbols) < min_symbols_per_commit:
            continue

        # FIX: generate one case per changed symbol, not just one per commit.
        # This eliminates the "first symbol always = query" selection bias.
        for i, query_sym in enumerate(all_changed_symbols):
            if len(cases) >= max_cases:
                break

            gt_syms = [s for j, s in enumerate(all_changed_symbols) if j != i]
            if not gt_syms:
                continue

            # Dedup: skip if we've already seen this exact query from any commit
            if query_sym in seen_queries:
                continue
            seen_queries.add(query_sym)

            case = CoChangeCase(
                commit_hash=commit_hash[:8],
                commit_msg=commit_msg[:80],
                changed_files=["./{}".format(f) for f in py_files],
                changed_symbols=all_changed_symbols,
                query_symbol=query_sym,
                ground_truth_symbols=gt_syms,
            )
            cases.append(case)

    return cases


if __name__ == "__main__":
    import sys
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    cases = extract_cochange_cases(repo, max_cases=50)
    print(f"Found {len(cases)} co-change test cases")
    for case in cases[:5]:
        print(f"\n  Commit: {case.commit_hash} — {case.commit_msg}")
        print(f"  Query:  {case.query_symbol}")
        print(f"  Ground truth ({len(case.ground_truth_symbols)}):")
        for gt in case.ground_truth_symbols[:5]:
            print(f"    {gt}")
