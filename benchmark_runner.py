"""
benchmark_runner.py - runs DiffContext on real repos, reports real metrics.

Usage:
    python benchmark_runner.py --repo /path/to/repo --changed ./api.py:get
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from benchmarks.evaluator import run_diffcontext
from repo_extractor import extract_repository_functions


def count_tokens(text):
    # ~4 chars per token (GPT approximation, good enough for reduction metrics)
    return max(1, len(text) // 4)


def full_repo_tokens(functions):
    all_code = "\n\n".join(f["code"] for f in functions.values())
    return count_tokens(all_code)


def selected_tokens(functions, selected_ids):
    code = "\n\n".join(
        functions[fid]["code"] for fid in selected_ids if fid in functions
    )
    return count_tokens(code)


def run_benchmark(repo_path, changed_functions, name="", note=""):
    repo_path = os.path.abspath(repo_path)

    print(f"\n{'=' * 60}")
    print(f"  {name or repo_path}")
    if note:
        print(f"  {note}")
    print(f"  Changed: {changed_functions}")
    print("=" * 60)

    functions = extract_repository_functions(repo_path)
    total_functions = len(functions)
    total_tokens = full_repo_tokens(functions)

    print(f"  Total functions : {total_functions}")
    print(f"  Full repo tokens: {total_tokens:,}")

    start = time.perf_counter()
    retrieved = run_diffcontext(repo_path, changed_functions)
    elapsed_ms = (time.perf_counter() - start) * 1000

    context_tokens = selected_tokens(functions, retrieved)
    token_reduction = (1 - context_tokens / total_tokens) * 100 if total_tokens else 0
    function_reduction = (1 - len(retrieved) / total_functions) * 100 if total_functions else 0

    print(f"\n  Retrieved {len(retrieved)} / {total_functions} functions:")
    for fid in sorted(retrieved):
        print(f"    {fid}")

    print(f"\n  Context tokens  : {context_tokens:,}")
    print(f"  Token reduction : {token_reduction:.1f}%")
    print(f"  Fn reduction    : {function_reduction:.1f}%")
    print(f"  Runtime         : {elapsed_ms:.1f} ms")

    return {
        "name": name,
        "changed": changed_functions,
        "total_functions": total_functions,
        "retrieved_count": len(retrieved),
        "retrieved_ids": sorted(retrieved),
        "total_tokens": total_tokens,
        "context_tokens": context_tokens,
        "token_reduction_pct": round(token_reduction, 2),
        "function_reduction_pct": round(function_reduction, 2),
        "runtime_ms": round(elapsed_ms, 1),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="Path to repo to benchmark")
    parser.add_argument("--changed", nargs="+", required=True,
                        help="Changed function IDs e.g. ./api.py:get")
    parser.add_argument("--name", default="", help="Label for this benchmark run")
    parser.add_argument("--out", default="benchmark_results.json")
    args = parser.parse_args()

    result = run_benchmark(args.repo, args.changed, name=args.name)

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()