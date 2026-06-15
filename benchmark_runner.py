"""
benchmark_runner.py - runs DiffContext on real repos, reports real metrics.

Usage:
    python benchmark_runner.py --repo /path/to/repo --changed ./api.py:get
    python benchmark_runner.py --repo https://github.com/psf/black --changed ./src/black/linegen.py:transform_line --name black_test --max-tokens 15000
"""

import argparse
import json
import os
import sys
import time
import tempfile
import subprocess
from typing import List, Dict, Any

sys.path.insert(0, os.path.dirname(__file__))

from benchmarks.evaluator import run_diffcontext
from repo_extractor import extract_repository_functions
from multi_file_dependency_graph import build_repository_graph
from relevance_scorer import score_relevance


def count_tokens(text: str) -> int:
    """~4 chars per token (GPT approximation, good enough for reduction metrics)"""
    return max(1, len(text) // 4)


def full_repo_tokens(functions: Dict) -> int:
    """Calculate total tokens for entire repository"""
    all_code = "\n\n".join(f["code"] for f in functions.values())
    return count_tokens(all_code)


def selected_tokens(functions: Dict, selected_ids: List[str]) -> int:
    """Calculate tokens for selected functions"""
    code = "\n\n".join(
        functions[fid]["code"] for fid in selected_ids if fid in functions
    )
    return count_tokens(code)


def filter_by_token_budget(
    functions: Dict,
    retrieved: List[str],
    changed_functions: List[str],
    graph: Dict,
    max_tokens: int = 10000
) -> List[str]:
    """
    Filter retrieved functions by token budget and relevance scoring.
    
    Priority order:
    1. Changed functions themselves (always included)
    2. Direct callees/callers (score >= 80)
    3. 2-hop relationships (score >= 50)
    4. Lower relevance (score >= 20) if budget permits
    """
    if max_tokens is None or max_tokens <= 0:
        return retrieved
    
    # Score each retrieved function
    scored = []
    for fn in retrieved:
        score = score_relevance(graph, changed_functions, fn)
        fn_tokens = selected_tokens(functions, [fn])
        scored.append((fn, score, fn_tokens))
    
    # Sort by relevance score (highest first)
    scored.sort(key=lambda x: x[1], reverse=True)
    
    # Apply token budget
    result = []
    current_tokens = 0
    changed_set = set(changed_functions)
    
    for fn, score, fn_tokens in scored:
        # Always include changed functions
        if fn in changed_set:
            result.append(fn)
            current_tokens += fn_tokens
        # Include high relevance regardless of budget
        elif score >= 80:
            result.append(fn)
            current_tokens += fn_tokens
        # Include if within budget
        elif current_tokens + fn_tokens <= max_tokens:
            result.append(fn)
            current_tokens += fn_tokens
        # Skip if over budget and low relevance
        else:
            print(f"    [SKIP] {fn} (score={score}, tokens={fn_tokens}) - over budget")
    
    return result


def run_benchmark(
    repo_path: str,
    changed_functions: List[str],
    name: str = "",
    note: str = "",
    max_depth: int = 2,
    max_tokens: int = 10000
) -> Dict[str, Any]:
    """
    Run benchmark with precision improvements.
    
    Args:
        repo_path: Local path or git URL
        changed_functions: List of function IDs that changed
        name: Label for this benchmark
        note: Optional note/description
        max_depth: Maximum dependency depth (1=direct, 2=one hop, None=full)
        max_tokens: Token budget for context (None = unlimited)
    """
    is_online = repo_path.startswith(("http://", "https://", "git@"))
    temp_dir = None
    
    if is_online:
        temp_dir = tempfile.mkdtemp()
        print(f"Cloning {repo_path} to {temp_dir}...")
        subprocess.run(["git", "clone", "--depth=1", repo_path, temp_dir], check=True)
        repo_path = temp_dir
    
    repo_path = os.path.abspath(repo_path)
    
    print(f"\n{'=' * 60}")
    print(f"  {name or repo_path}")
    if note:
        print(f"  {note}")
    print(f"  Changed: {changed_functions}")
    print(f"  Max depth: {max_depth if max_depth else 'full'}")
    print(f"  Max tokens: {max_tokens if max_tokens else 'unlimited'}")
    print("=" * 60)
    
    # Extract all functions
    functions = extract_repository_functions(repo_path)
    total_functions = len(functions)
    total_tokens = full_repo_tokens(functions)
    
    print(f"  Total functions : {total_functions}")
    print(f"  Full repo tokens: {total_tokens:,}")
    
    # Build graph (once)
    print(f"  Building dependency graph...")
    start_graph = time.perf_counter()
    graph = build_repository_graph(repo_path)
    graph_time = (time.perf_counter() - start_graph) * 1000
    
    edges = sum(len(deps) for deps in graph.values())
    print(f"  Graph: {len(graph)} nodes, {edges} edges in {graph_time:.1f}ms")
    
    # Run pipeline with depth limiting
    start = time.perf_counter()
    retrieved = run_diffcontext(repo_path, changed_functions, max_depth=max_depth)
    elapsed_ms = (time.perf_counter() - start) * 1000
    
    print(f"\n  Raw retrieval: {len(retrieved)} functions")
    
    # Apply token budget filtering
    if max_tokens:
        retrieved = filter_by_token_budget(
            functions, retrieved, changed_functions, graph, max_tokens
        )
        print(f"  After budget: {len(retrieved)} functions")
    
    # Calculate metrics
    context_tokens = selected_tokens(functions, retrieved)
    token_reduction = (1 - context_tokens / total_tokens) * 100 if total_tokens else 0
    function_reduction = (1 - len(retrieved) / total_functions) * 100 if total_functions else 0
    
    # Calculate precision if ground truth available
    precision_note = ""
    if name and "test" in name.lower():
        # Simple precision check for known patterns
        expected_callees = []
        for changed in changed_functions:
            expected_callees.extend(graph.get(changed, []))
        
        if expected_callees:
            found_callees = [c for c in expected_callees if c in retrieved]
            precision = len(found_callees) / len(retrieved) if retrieved else 0
            recall = len(found_callees) / len(expected_callees) if expected_callees else 0
            precision_note = f" | Precision: {precision:.2f}, Recall: {recall:.2f}"
    
    print(f"\n  Retrieved {len(retrieved)} / {total_functions} functions:")
    for fid in sorted(retrieved)[:20]:  # Show first 20
        print(f"    {fid}")
    if len(retrieved) > 20:
        print(f"    ... and {len(retrieved) - 20} more")
    
    print(f"\n  Context tokens  : {context_tokens:,}")
    print(f"  Token reduction : {token_reduction:.1f}%")
    print(f"  Fn reduction    : {function_reduction:.1f}%")
    print(f"  Runtime         : {elapsed_ms:.1f}ms (graph: {graph_time:.1f}ms){precision_note}")
    
    # Cleanup temp directory if we cloned
    if temp_dir and os.path.exists(temp_dir):
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    return {
        "name": name,
        "note": note,
        "changed": changed_functions,
        "max_depth": max_depth,
        "max_tokens": max_tokens,
        "total_functions": total_functions,
        "retrieved_count": len(retrieved),
        "retrieved_ids": sorted(retrieved),
        "total_tokens": total_tokens,
        "context_tokens": context_tokens,
        "token_reduction_pct": round(token_reduction, 2),
        "function_reduction_pct": round(function_reduction, 2),
        "runtime_ms": round(elapsed_ms, 1),
        "graph_build_ms": round(graph_time, 1),
        "graph_nodes": len(graph),
        "graph_edges": edges,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run DiffContext benchmark on real repositories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Local repo with default settings
  python benchmark_runner.py --repo ./myproject --changed ./api.py:get_user
  
  # Remote repo with depth limit
  python benchmark_runner.py --repo https://github.com/psf/black \\
      --changed ./src/black/linegen.py:transform_line --max-depth 1
  
  # With token budget
  python benchmark_runner.py --repo ./flask --changed ./app.py:Flask.route \\
      --max-tokens 5000 --name flask_test
        """
    )
    parser.add_argument("--repo", required=True, help="Path or git URL to repo")
    parser.add_argument("--changed", nargs="+", required=True,
                        help="Changed function IDs e.g. ./api.py:get")
    parser.add_argument("--name", default="", help="Label for this benchmark run")
    parser.add_argument("--note", default="", help="Optional description")
    parser.add_argument("--max-depth", type=int, default=2,
                        help="Max dependency depth (1=direct, 2=one hop, default=2)")
    parser.add_argument("--max-tokens", type=int, default=10000,
                        help="Token budget for context (default=10000, 0=unlimited)")
    parser.add_argument("--out", default="benchmark_results.json",
                        help="Output JSON file")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed output")
    
    args = parser.parse_args()
    
    # Handle max_tokens=0 as unlimited
    max_tokens = args.max_tokens if args.max_tokens > 0 else None
    
    result = run_benchmark(
        args.repo,
        args.changed,
        name=args.name,
        note=args.note,
        max_depth=args.max_depth,
        max_tokens=max_tokens
    )
    
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n✓ Results saved to {args.out}")
    
    # Print summary
    print(f"\n{'=' * 60}")
    print("  SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Repository: {result['name'] or args.repo}")
    print(f"  Functions: {result['retrieved_count']}/{result['total_functions']} "
          f"({result['function_reduction_pct']:.1f}% reduction)")
    print(f"  Tokens: {result['context_tokens']:,}/{result['total_tokens']:,} "
          f"({result['token_reduction_pct']:.1f}% reduction)")
    print(f"  Runtime: {result['runtime_ms']:.1f}ms")


if __name__ == "__main__":
    main()