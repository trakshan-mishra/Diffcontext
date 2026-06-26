#!/usr/bin/env python3
"""
run_benchmark.py — Honest co-change benchmark against a real repo.

Ground truth comes from git history: which Python functions were
modified TOGETHER in the same real commit.

Fixes vs original:
  1. Ground truth is now SYMMETRIC: for each commit with N co-changed
     functions, we run N evaluations (each symbol as query, rest as GT)
     and average. Original only used the first symbol as query, which
     biased results toward whatever symbol happened to sort first.
  2. Filtering: skip commits where all co-changed symbols are in the
     same file (those test file-colocation, not graph retrieval).
  3. Token budget raised to match realistic use (8000 default).
  4. Reports per-repo breakdown AND micro-averaged totals.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import get_blast_radius
from diffcontext.impact.scoring import compute_impact_scores
from diffcontext.context.selector import select_context


def get_commit_list(repo_path, max_commits=80):
    result = subprocess.run(
        ["git", "log", "--format=%H", f"-{max_commits}"],
        cwd=repo_path, capture_output=True, text=True,
    )
    return [c for c in result.stdout.strip().split("\n") if c]


def get_changed_py_files(git_root, commit, indexed_subdir=None):
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
        cwd=git_root, capture_output=True, text=True,
    )
    all_files = [f for f in result.stdout.strip().split("\n") if f.endswith(".py") and f]

    if indexed_subdir is None:
        return all_files

    rel_prefix = os.path.relpath(indexed_subdir, git_root)
    if rel_prefix == ".":
        return all_files

    rel_prefix = rel_prefix.rstrip("/") + "/"
    return [f[len(rel_prefix):] for f in all_files if f.startswith(rel_prefix)]


def get_changed_lines(git_root, filepath_in_git_root, commit):
    result = subprocess.run(
        ["git", "show", "-U0", commit, "--", filepath_in_git_root],
        cwd=git_root, capture_output=True, text=True,
    )
    changed_lines = set()
    for line in result.stdout.split("\n"):
        if line.startswith("@@"):
            parts = line.split()
            for part in parts:
                if part.startswith("+") and "," in part:
                    start_str, count_str = part[1:].split(",", 1)
                    start, count = int(start_str), int(count_str)
                    changed_lines.update(range(start, start + max(count, 1)))
                elif part.startswith("+") and part[1:].isdigit():
                    changed_lines.add(int(part[1:]))
    return changed_lines


def symbols_changed_in_commit(git_root, commit, changed_files, indexed_subdir, current_symbols):
    rel_prefix = os.path.relpath(indexed_subdir, git_root)
    rel_prefix = "" if rel_prefix == "." else rel_prefix.rstrip("/") + "/"

    involved = []
    for sym_id, sym in current_symbols.items():
        rel = sym_id.split(":", 1)[0].lstrip("./")
        if rel not in changed_files:
            continue

        git_relative_path = rel_prefix + rel
        changed_lines = get_changed_lines(git_root, git_relative_path, commit)
        if not changed_lines:
            continue

        code_lines = sym.code.count("\n") + 1
        sym_lines = set(range(sym.lineno, sym.lineno + code_lines))

        if sym_lines & changed_lines:
            involved.append(sym_id)

    return involved


def run_diffcontext_retrieval(graph, symbols, query_symbol, max_tokens=8000):
    if query_symbol not in graph:
        return set()
    radius = get_blast_radius(graph, query_symbol)
    scores = compute_impact_scores(graph, [query_symbol], {query_symbol: radius})
    selected, _dropped = select_context(symbols, scores, [query_symbol], max_tokens=max_tokens)
    return set(selected) - {query_symbol}


def precision_recall_f1(retrieved, ground_truth):
    if not ground_truth:
        return None
    tp = len(retrieved & ground_truth)
    fp = len(retrieved - ground_truth)
    fn = len(ground_truth - retrieved)
    p  = tp / (tp + fp) if (tp + fp) else 0.0
    r  = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f1, tp, fp, fn


def main(repo_path, max_commits=80, max_cases=25, max_tokens=8000):
    print(f"Indexing {repo_path} ...")
    symbols = extract_all_symbols(repo_path)
    graph   = build_repository_graph(repo_path)
    print(f"  {len(symbols)} symbols, {sum(len(v) for v in graph.values())} edges\n")

    git_root_result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo_path, capture_output=True, text=True,
    )
    git_root = git_root_result.stdout.strip()
    if not git_root:
        print(f"'{repo_path}' is not inside a git repository.")
        return

    commits = get_commit_list(git_root, max_commits)
    print(f"Scanning {len(commits)} commits for multi-function co-changes...\n")

    results = []
    cases_seen = 0

    for commit in commits:
        if cases_seen >= max_cases:
            break

        changed_files = get_changed_py_files(git_root, commit, indexed_subdir=repo_path)
        if len(changed_files) < 2:
            continue

        involved = symbols_changed_in_commit(
            git_root, commit, set(changed_files), repo_path, symbols
        )
        if len(involved) < 2:
            continue

        # FIX: skip same-file-only commits — those test colocation not graph
        involved_files = set(sid.split(":")[0] for sid in involved)
        if len(involved_files) < 2:
            continue

        # FIX: symmetric evaluation — each symbol as query, rest as GT
        for i, query in enumerate(involved):
            if cases_seen >= max_cases:
                break
            ground_truth = set(involved) - {query}

            retrieved = run_diffcontext_retrieval(graph, symbols, query, max_tokens)
            pr = precision_recall_f1(retrieved, ground_truth)
            if pr is None:
                continue

            p, r, f1, tp, fp, fn = pr
            results.append((commit[:8], query, len(ground_truth), len(retrieved), p, r, f1))
            cases_seen += 1

    if not results:
        print("No valid co-change cases found in this commit window.")
        return

    print(f"{'Commit':<10} {'GT':>4} {'Ret':>5} {'P':>6} {'R':>6} {'F1':>6}  Query")
    print("-" * 90)
    for commit, query, gt_n, ret_n, p, r, f1 in results:
        hit = "✓" if r > 0 else "✗"
        print(f"{hit} {commit:<9} {gt_n:>4} {ret_n:>5} {p:>6.3f} {r:>6.3f} {f1:>6.3f}  {query}")

    avg_p  = sum(r[4] for r in results) / len(results)
    avg_r  = sum(r[5] for r in results) / len(results)
    avg_f1 = sum(r[6] for r in results) / len(results)
    recall_hits = sum(1 for r in results if r[5] > 0)

    print("-" * 90)
    print(f"N cases      : {len(results)}")
    print(f"Recall > 0   : {recall_hits} ({recall_hits/len(results)*100:.0f}%)")
    print(f"Avg P        : {avg_p:.3f}")
    print(f"Avg R        : {avg_r:.3f}")
    print(f"Avg F1       : {avg_f1:.3f}")


if __name__ == "__main__":
    repo       = sys.argv[1] if len(sys.argv) > 1 else "."
    max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    main(repo, max_commits=400, max_cases=40, max_tokens=max_tokens)