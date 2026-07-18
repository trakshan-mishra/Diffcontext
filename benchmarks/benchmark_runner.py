#!/usr/bin/env python3
"""
benchmark_runner.py — Research-grade DiffContext benchmarks.

Three evaluation modes:
1. CO-CHANGE (default): Ground truth from git history — real human behavior
2. GRAPH: Internal consistency check (circular, for debugging only)
3. BASELINES: Compare DiffContext vs BM25 vs file-colocation vs random

Usage (from the repo root):
    # Clone real repos with full history first:
    python benchmarks/benchmark_runner.py --clone

    # Run co-change benchmark (honest, non-circular):
    python benchmarks/benchmark_runner.py --cochange

    # Compare against baselines:
    python benchmarks/benchmark_runner.py --compare

    # Run everything:
    python benchmarks/benchmark_runner.py --full

    # Single repo:
    python benchmarks/benchmark_runner.py --repo /path/to/repo --cochange
"""

import argparse
import json
import os
import subprocess
import sys
import time
import random
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import get_blast_radius
from diffcontext.impact.traversal import expand_dependencies
from diffcontext.impact.scoring import compute_impact_scores
from diffcontext.context.selector import select_context
from diffcontext.models import Symbol

from benchmarks.ground_truth import extract_cochange_cases, CoChangeCase
from benchmarks.baselines import BM25Baseline, FileCoLocationBaseline, RandomBaseline


# ---- Real repos to benchmark against ----
BENCHMARK_REPOS = {
    "flask": {
        "url": "https://github.com/pallets/flask.git",
        "description": "Micro web framework (354 symbols)",
    },
    "click": {
        "url": "https://github.com/pallets/click.git",
        "description": "CLI creation toolkit (506 symbols)",
    },
    "httpx": {
        "url": "https://github.com/encode/httpx.git",
        "description": "HTTP client (large, async-heavy)",
    },
    "pydantic": {
        "url": "https://github.com/pydantic/pydantic.git",
        "description": "Data validation (heavy inheritance)",
    },
}


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ---- Clone repos ----

def clone_repos(base_dir: str, repos: Optional[List[str]] = None):
    """Clone real repos with full git history for co-change analysis."""
    os.makedirs(base_dir, exist_ok=True)

    targets = repos or list(BENCHMARK_REPOS.keys())
    for name in targets:
        if name not in BENCHMARK_REPOS:
            print(f"Unknown repo: {name}")
            continue

        repo_dir = os.path.join(base_dir, name)
        if os.path.isdir(repo_dir):
            print(f"  {name}: already exists, skipping")
            continue

        url = BENCHMARK_REPOS[name]["url"]
        print(f"  Cloning {name} from {url}...")
        result = subprocess.run(
            ["git", "clone", "--depth=100", url, repo_dir],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            print(f"  ✓ {name} cloned")
        else:
            print(f"  ✗ {name} failed: {result.stderr[:200]}")


# ---- DiffContext retrieval ----

def run_diffcontext(
    repo_path: str,
    changed_symbols: List[str],
    graph: Dict[str, List[str]],
    symbols: Dict[str, Symbol],
    max_depth: int = 2,
    max_tokens: int = 10000,
) -> List[str]:
    """Run DiffContext pipeline, return retrieved symbol IDs."""
    valid = [s for s in changed_symbols if s in graph]
    if not valid:
        return []

    # Blast radius
    blast_radii = {}
    all_blast = []
    for sym_id in valid:
        radius = get_blast_radius(graph, sym_id)
        blast_radii[sym_id] = radius
        all_blast.extend(radius)

    # Expand
    seed = list(set(valid + all_blast))
    expanded = expand_dependencies(graph, seed, max_depth=max_depth)

    # Score
    scores = compute_impact_scores(graph, valid, blast_radii)

    # Select
    selected, _dropped = select_context(symbols, scores, valid, max_tokens=max_tokens)

    return [s for s in selected if s not in set(valid)]


# ---- Metrics ----

def compute_metrics(
    retrieved: Set[str],
    ground_truth: Set[str],
) -> Dict[str, float]:
    """Standard precision/recall/F1."""
    if not ground_truth:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "tp": 0, "fp": 0, "fn": 0}

    tp = len(retrieved & ground_truth)
    fp = len(retrieved - ground_truth)
    fn = len(ground_truth - retrieved)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


# ---- Co-change benchmark ----

def run_cochange_benchmark(
    repo_path: str,
    repo_name: str = "",
    max_cases: int = 20,
    max_tokens: int = 10000,
) -> Dict:
    """
    Run benchmark using git co-change ground truth.

    This is the HONEST evaluation — ground truth comes from
    human developer behavior (which functions they changed together),
    NOT from the dependency graph.
    """
    repo_path = os.path.abspath(repo_path)
    name = repo_name or os.path.basename(repo_path)

    print(f"\n{'=' * 60}")
    print(f"  CO-CHANGE BENCHMARK: {name}")
    print(f"  (Ground truth from git history — non-circular)")
    print(f"{'=' * 60}")

    # 1. Extract co-change cases from git history
    print(f"  Extracting co-change cases from git history...")
    cases = extract_cochange_cases(repo_path, max_cases=max_cases)

    if not cases:
        print(f"  No co-change cases found. Need full git history (not shallow clone).")
        print(f"  Run: python benchmark_runner.py --clone")
        return {"name": name, "error": "no_cases", "cases": []}

    print(f"  Found {len(cases)} co-change cases")

    # 2. Index repo
    print(f"  Indexing repository...")
    t0 = time.perf_counter()
    symbols = extract_all_symbols(repo_path)
    graph = build_repository_graph(repo_path)
    index_ms = (time.perf_counter() - t0) * 1000

    total_symbols = len(symbols)
    total_edges = sum(len(deps) for deps in graph.values())
    print(f"  {total_symbols} symbols, {total_edges} edges ({index_ms:.0f}ms)")

    # 3. Run each case
    results = []
    total_p = total_r = total_f1 = 0
    valid_cases = 0

    for i, case in enumerate(cases):
        # Check query exists in current code
        if case.query_symbol not in graph:
            continue

        # Ground truth = other symbols changed in same commit
        gt = set(case.ground_truth_symbols)
        # Only keep GT symbols that exist in current codebase
        gt = gt & set(graph.keys())
        if not gt:
            continue

        # Run DiffContext
        retrieved = run_diffcontext(
            repo_path, [case.query_symbol], graph, symbols,
            max_tokens=max_tokens,
        )
        retrieved_set = set(retrieved)

        metrics = compute_metrics(retrieved_set, gt)

        results.append({
            "commit": case.commit_hash,
            "msg": case.commit_msg[:50],
            "query": case.query_symbol,
            "gt_size": len(gt),
            "retrieved_size": len(retrieved),
            **metrics,
        })

        total_p += metrics["precision"]
        total_r += metrics["recall"]
        total_f1 += metrics["f1"]
        valid_cases += 1

        hit = "✓" if metrics["recall"] > 0 else "✗"
        print(f"  {hit} [{case.commit_hash}] P={metrics['precision']:.2f} "
              f"R={metrics['recall']:.2f} F1={metrics['f1']:.2f} "
              f"(GT={len(gt)}, ret={len(retrieved)})")

    if valid_cases == 0:
        print(f"  No valid test cases (symbols may have been refactored)")
        return {"name": name, "error": "no_valid_cases", "cases": []}

    avg_p = total_p / valid_cases
    avg_r = total_r / valid_cases
    avg_f1 = total_f1 / valid_cases

    print(f"\n  {'─' * 40}")
    print(f"  Average over {valid_cases} cases:")
    print(f"  Precision : {avg_p:.3f}")
    print(f"  Recall    : {avg_r:.3f}")
    print(f"  F1        : {avg_f1:.3f}")

    return {
        "name": name,
        "total_symbols": total_symbols,
        "total_edges": total_edges,
        "valid_cases": valid_cases,
        "avg_precision": round(avg_p, 4),
        "avg_recall": round(avg_r, 4),
        "avg_f1": round(avg_f1, 4),
        "cases": results,
    }


# ---- Baseline comparison ----

def run_baseline_comparison(
    repo_path: str,
    repo_name: str = "",
    max_cases: int = 15,
    max_tokens: int = 10000,
) -> Dict:
    """
    Compare DiffContext against BM25, file co-location, and random baselines.

    Uses co-change ground truth for all methods.
    """
    repo_path = os.path.abspath(repo_path)
    name = repo_name or os.path.basename(repo_path)

    print(f"\n{'=' * 60}")
    print(f"  BASELINE COMPARISON: {name}")
    print(f"  DiffContext vs BM25 vs File-CoLocation vs Random")
    print(f"{'=' * 60}")

    # Extract test cases
    cases = extract_cochange_cases(repo_path, max_cases=max_cases)
    if not cases:
        print(f"  No co-change cases. Need full git history.")
        return {"name": name, "error": "no_cases"}

    # Index
    symbols = extract_all_symbols(repo_path)
    graph = build_repository_graph(repo_path)
    print(f"  {len(symbols)} symbols, {sum(len(d) for d in graph.values())} edges")

    # Build baselines
    print(f"  Building BM25 index...")
    bm25 = BM25Baseline(symbols)
    coloc = FileCoLocationBaseline(symbols)
    rand = RandomBaseline(symbols)

    methods = {
        "DiffContext": lambda q: run_diffcontext(repo_path, [q], graph, symbols, max_tokens=max_tokens),
        "BM25": lambda q: bm25.retrieve(q, top_k=30),
        "File-CoLoc": lambda q: coloc.retrieve(q, top_k=30),
        "Random": lambda q: rand.retrieve(q, top_k=30),
    }

    # Run each case with each method
    scores = {m: {"p": 0, "r": 0, "f1": 0, "n": 0} for m in methods}

    for case in cases:
        if case.query_symbol not in graph:
            continue

        gt = set(case.ground_truth_symbols) & set(graph.keys())
        if not gt:
            continue

        for method_name, method_fn in methods.items():
            retrieved = set(method_fn(case.query_symbol))
            metrics = compute_metrics(retrieved, gt)

            scores[method_name]["p"] += metrics["precision"]
            scores[method_name]["r"] += metrics["recall"]
            scores[method_name]["f1"] += metrics["f1"]
            scores[method_name]["n"] += 1

    # Print results
    print(f"\n  {'Method':<15} {'Precision':>10} {'Recall':>10} {'F1':>10} {'Cases':>8}")
    print(f"  {'─' * 55}")

    summary = {}
    for method_name, s in scores.items():
        n = s["n"]
        if n == 0:
            continue
        avg_p = s["p"] / n
        avg_r = s["r"] / n
        avg_f1 = s["f1"] / n
        print(f"  {method_name:<15} {avg_p:>10.3f} {avg_r:>10.3f} {avg_f1:>10.3f} {n:>8}")
        summary[method_name] = {
            "precision": round(avg_p, 4),
            "recall": round(avg_r, 4),
            "f1": round(avg_f1, 4),
            "cases": n,
        }

    return {"name": name, "methods": summary}


# ---- Graph consistency check (the old benchmark) ----

def run_graph_benchmark(
    repo_path: str,
    repo_name: str = "",
    num_symbols: int = 3,
    max_tokens: int = 10000,
) -> Dict:
    """
    Internal consistency check — graph-derived ground truth.
    CIRCULAR — useful for debugging, NOT for proving the algorithm works.
    """
    repo_path = os.path.abspath(repo_path)
    name = repo_name or os.path.basename(repo_path)

    print(f"\n  GRAPH CONSISTENCY: {name} (circular — debug only)")

    symbols = extract_all_symbols(repo_path)
    graph = build_repository_graph(repo_path)

    # Build reverse graph
    reverse: Dict[str, Set[str]] = {}
    for caller, callees in graph.items():
        for callee in callees:
            reverse.setdefault(callee, set()).add(caller)

    # Pick high-connectivity symbols
    scored = []
    for sym_id in graph:
        indegree = len(reverse.get(sym_id, set()))
        outdegree = len(graph.get(sym_id, []))
        if indegree + outdegree > 0:
            scored.append((sym_id, indegree + outdegree))

    scored.sort(key=lambda x: x[1], reverse=True)
    test_symbols = [s[0] for s in scored[:num_symbols]]

    results = []
    for sym_id in test_symbols:
        # Ground truth = 2-hop neighborhood
        gt = set()
        # Forward 2-hop
        frontier = [sym_id]
        visited = {sym_id}
        for _ in range(2):
            next_f = []
            for s in frontier:
                for callee in graph.get(s, []):
                    if callee not in visited:
                        visited.add(callee)
                        gt.add(callee)
                        next_f.append(callee)
            frontier = next_f
        # Reverse 2-hop
        frontier = [sym_id]
        visited_r = {sym_id}
        for _ in range(2):
            next_f = []
            for s in frontier:
                for caller in reverse.get(s, set()):
                    if caller not in visited_r:
                        visited_r.add(caller)
                        gt.add(caller)
                        next_f.append(caller)
            frontier = next_f

        # Run pipeline
        retrieved = run_diffcontext(
            repo_path, [sym_id], graph, symbols,
            max_tokens=max_tokens,
        )

        metrics = compute_metrics(set(retrieved), gt)
        results.append({"symbol": sym_id, **metrics})

        print(f"    {sym_id.split(':')[-1]:<35} "
              f"P={metrics['precision']:.3f} R={metrics['recall']:.3f} F1={metrics['f1']:.3f}")

    return {"name": name, "type": "graph_consistency", "results": results}


# ---- Full suite ----

def run_full_suite(bench_dir: str):
    """Run all benchmarks on all available repos."""
    all_results = {}

    for name in sorted(os.listdir(bench_dir)):
        repo_path = os.path.join(bench_dir, name)
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            continue

        print(f"\n\n{'#' * 60}")
        print(f"  REPO: {name}")
        print(f"{'#' * 60}")

        # Co-change benchmark (the real test)
        cochange = run_cochange_benchmark(repo_path, name)

        # Baseline comparison
        baseline = run_baseline_comparison(repo_path, name)

        # Graph consistency (debug)
        graph_check = run_graph_benchmark(repo_path, name)

        all_results[name] = {
            "cochange": cochange,
            "baseline": baseline,
            "graph_consistency": graph_check,
        }

    # Save
    out_path = os.path.join(os.path.dirname(__file__), "benchmark_results.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n\n✓ All results saved to {out_path}")

    # Print final summary
    print(f"\n{'=' * 60}")
    print(f"  FINAL SUMMARY")
    print(f"{'=' * 60}")
    print(f"  {'Repo':<15} {'Co-change F1':>14} {'vs BM25':>10} {'vs Random':>10}")
    print(f"  {'─' * 50}")

    for name, data in all_results.items():
        cc = data.get("cochange", {})
        bl = data.get("baseline", {}).get("methods", {})

        cc_f1 = cc.get("avg_f1", 0)
        bm25_f1 = bl.get("BM25", {}).get("f1", 0)
        rand_f1 = bl.get("Random", {}).get("f1", 0)

        diff_bm25 = f"+{cc_f1 - bm25_f1:.3f}" if bm25_f1 else "N/A"
        diff_rand = f"+{cc_f1 - rand_f1:.3f}" if rand_f1 else "N/A"

        print(f"  {name:<15} {cc_f1:>14.3f} {diff_bm25:>10} {diff_rand:>10}")


def main():
    parser = argparse.ArgumentParser(
        description="DiffContext Research-Grade Benchmarks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Step-by-step guide:

  1. Clone real repos:
     python benchmark_runner.py --clone

  2. Run co-change benchmark (non-circular, honest):
     python benchmark_runner.py --cochange

  3. Compare against baselines (BM25, random):
     python benchmark_runner.py --compare

  4. Run everything:
     python benchmark_runner.py --full

  5. Single repo:
     python benchmark_runner.py --repo /path/to/repo --cochange
        """,
    )
    parser.add_argument("--clone", action="store_true",
                        help="Clone benchmark repos with git history")
    parser.add_argument("--cochange", action="store_true",
                        help="Run co-change benchmark (honest ground truth)")
    parser.add_argument("--compare", action="store_true",
                        help="Compare DiffContext vs baselines")
    parser.add_argument("--graph", action="store_true",
                        help="Run graph consistency check (circular, debug only)")
    parser.add_argument("--full", action="store_true",
                        help="Run all benchmarks")
    parser.add_argument("--repo", help="Path to specific repo")
    parser.add_argument("--bench-dir", default=None,
                        help="Directory containing benchmark repos")
    parser.add_argument("--max-cases", type=int, default=20,
                        help="Max test cases per repo")
    parser.add_argument("--max-tokens", type=int, default=10000,
                        help="Token budget")

    args = parser.parse_args()

    # Default bench dir: <repo root>/benchmark_repos, same as the other
    # benchmark scripts
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bench_dir = args.bench_dir or os.path.join(base, "benchmark_repos")

    if args.clone:
        print("Cloning benchmark repos with full git history...\n")
        clone_repos(bench_dir)
        return

    if args.repo:
        repo_path = os.path.abspath(args.repo)
        name = os.path.basename(repo_path)

        if args.cochange or args.full:
            run_cochange_benchmark(repo_path, name, args.max_cases, args.max_tokens)
        if args.compare or args.full:
            run_baseline_comparison(repo_path, name, args.max_cases, args.max_tokens)
        if args.graph or args.full:
            run_graph_benchmark(repo_path, name)
        if not (args.cochange or args.compare or args.graph or args.full):
            # Default: run co-change
            run_cochange_benchmark(repo_path, name, args.max_cases, args.max_tokens)
        return

    if args.full:
        if not os.path.isdir(bench_dir):
            print(f"No repos found at {bench_dir}")
            print("Run: python benchmark_runner.py --clone")
            return
        run_full_suite(bench_dir)
        return

    # Default: run co-change on all available repos
    if os.path.isdir(bench_dir):
        for name in sorted(os.listdir(bench_dir)):
            repo_path = os.path.join(bench_dir, name)
            if os.path.isdir(os.path.join(repo_path, ".git")):
                if args.cochange or not (args.compare or args.graph):
                    run_cochange_benchmark(repo_path, name, args.max_cases, args.max_tokens)
                if args.compare:
                    run_baseline_comparison(repo_path, name, args.max_cases, args.max_tokens)
                if args.graph:
                    run_graph_benchmark(repo_path, name)
    else:
        # Try the existing benchmarks/ repos
        old_bench = os.path.join(base, "benchmarks")
        found = False
        for name in ["flask", "fastapi", "click"]:
            repo_path = os.path.join(old_bench, name)
            if os.path.isdir(repo_path):
                found = True
                if args.graph:
                    run_graph_benchmark(repo_path, name)
                else:
                    print(f"\n  {name}: No git history available for co-change benchmark.")
                    print(f"  Run: python benchmark_runner.py --clone")
                    run_graph_benchmark(repo_path, name)

        if not found:
            print("No benchmark repos found.")
            print("Run: python benchmark_runner.py --clone")


if __name__ == "__main__":
    main()