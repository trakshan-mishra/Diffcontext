#!/usr/bin/env python3
"""
context_reduction.py — how much context does DiffContext actually save,
and what does it keep?

The retrieval-quality benchmarks (eval_v1/eval_v2_hardened) answer "does it
find the right functions". This one answers the question a reader asks first:
"how big is the prompt, before and after?" — and pairs every reduction number
with the recall it was bought at, so the reduction can't be read alone.

Unit of measurement is ONE QUERY: a developer changed one function, and asks
for the context needed to change it safely. That is the same unit
eval_v2_hardened uses for its per-symbol rows, and the ground truth is the
same mined co-change set, so precision/recall printed here should reproduce
the published per-symbol table.

Definitions (all stated, none inferred):
  total_functions    every function/method DiffContext indexes at HEAD.
  full_repo_tokens   sum of token cost of every indexed function's source.
                     This is the "paste the whole codebase" denominator —
                     it EXCLUDES module-level code, comments outside
                     function bodies, and non-Python files, so it is a
                     conservative (smaller) denominator than a real
                     cat-the-repo prompt.
  retrieved_*        mean over queries of the ranked, budget-truncated
                     context set the product would compile.
  token cost         len(source) // 4, the same estimator
                     benchmarks/eval_v1.truncate_by_token_budget budgets
                     with. Reduction is a RATIO of two code measurements,
                     so it is insensitive to tokenizer choice; the absolute
                     token counts are estimates, not tiktoken output.

Usage:
  python benchmarks/context_reduction.py                    # all repos
  python benchmarks/context_reduction.py benchmark_repos/flask
  python benchmarks/context_reduction.py --commits 40       # faster pass
"""

import argparse
import json
import os
import statistics
import sys
import time
from typing import Dict, List, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import build_reverse_graph

from benchmarks.baselines import BM25Baseline
from benchmarks.eval_v1 import TOKEN_BUDGET, truncate_by_token_budget
from benchmarks.eval_v2_hardened import (
    TARGET_COMMITS, _hybrid_variants, _repo_head_sha, case_metrics,
    extract_distinct_commits,
)

RESULTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "results", "context_reduction"
)

# The repos the retrieval benchmark reports, in the order the README shows
# them: five tuning repos first, then the four held-out validation repos.
DEFAULT_REPOS = ["django", "flask", "click", "httpx", "pydantic",
                 "black", "requests", "rich", "starlette"]
VALIDATION_REPOS = {"black", "requests", "rich", "starlette"}


def token_cost(code: str) -> int:
    """Token estimate for a chunk of source — identical to the estimator the
    budget truncation uses, so 'retrieved tokens' and 'the budget that
    produced this retrieval' are measured on one scale."""
    return max(1, len(code) // 4)


def measure_repo(repo_path: str, target_commits: int = TARGET_COMMITS) -> Optional[Dict]:
    repo_name = os.path.basename(os.path.abspath(repo_path))
    print(f"\n{'=' * 68}\n  {repo_name}\n{'=' * 68}")

    # ── Repo-level totals ────────────────────────────────────────────────
    t0 = time.perf_counter()
    symbols = extract_all_symbols(repo_path)
    parse_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    graph = build_repository_graph(repo_path)
    reverse_graph = build_reverse_graph(graph)
    graph_s = time.perf_counter() - t0

    total_functions = len(symbols)
    full_repo_tokens = sum(token_cost(s.code) for s in symbols.values())
    files = {sid.split(":")[0] for sid in symbols}
    print(f"  {total_functions:,} functions across {len(files):,} files "
          f"= {full_repo_tokens:,} est. tokens")
    print(f"  index: parse {parse_s:.1f}s + graph {graph_s:.1f}s")

    # ── Ground truth: same mining as eval_v2_hardened ────────────────────
    t0 = time.perf_counter()
    commits = extract_distinct_commits(repo_path, target=target_commits)
    sset = set(symbols)
    valid = [(c, vs) for c in commits
             if len(vs := [s for s in c.symbols if s in sset]) >= 2]
    print(f"  {len(valid)} valid commits mined in {time.perf_counter() - t0:.0f}s")
    if not valid:
        return None

    symbol_ids = list(symbols)
    bm25 = BM25Baseline(symbols)

    # ── Per-query retrieval ──────────────────────────────────────────────
    rows: List[Dict] = []
    for c, vsyms in valid:
        for q in vsyms:
            gt: Set[str] = set(vsyms) - {q}
            t_q = time.perf_counter()
            ranked = _hybrid_variants(
                q, symbols, symbol_ids, graph, reverse_graph, bm25
            )["hybrid"]
            query_ms = (time.perf_counter() - t_q) * 1000

            retrieved_tokens = sum(token_cost(symbols[s].code)
                                   for s in ranked if s in symbols)
            m = case_metrics(ranked, gt)
            rows.append({
                "commit": c.commit_hash,
                "query": q,
                "gt_size": len(gt),
                "retrieved_n": len(ranked),
                "retrieved_tokens": retrieved_tokens,
                "query_ms": query_ms,
                "precision": m["precision"],
                "recall": m["recall"],
                "hit": m["hit"],
            })

    def mean(key: str) -> float:
        return statistics.mean(r[key] for r in rows)

    # Per-commit aggregate: mean within a commit, then across commits — the
    # unit the published headline table uses (a commit counts once, so
    # commits that touched many functions don't dominate).
    by_commit: Dict[str, List[Dict]] = {}
    for r in rows:
        by_commit.setdefault(r["commit"], []).append(r)

    def commit_mean(key: str) -> float:
        return statistics.mean(
            statistics.mean(r[key] for r in group) for group in by_commit.values()
        )

    retrieved_tokens = mean("retrieved_tokens")
    reduction_pct = 100.0 * (1 - retrieved_tokens / full_repo_tokens)

    result = {
        "repo": repo_name,
        "held_out": repo_name in VALIDATION_REPOS,
        "repo_head_sha": _repo_head_sha(repo_path),
        "n_commits": len(valid),
        "n_queries": len(rows),
        "total_functions": total_functions,
        "total_files": len(files),
        "full_repo_tokens": full_repo_tokens,
        "retrieved_functions": round(mean("retrieved_n"), 1),
        "retrieved_tokens": round(retrieved_tokens),
        "token_reduction_pct": round(reduction_pct, 2),
        "budget_utilization_pct": round(100.0 * retrieved_tokens / TOKEN_BUDGET, 1),
        "precision": round(mean("precision"), 4),
        "recall": round(mean("recall"), 4),
        "hit": round(mean("hit"), 4),
        "precision_per_commit": round(commit_mean("precision"), 4),
        "recall_per_commit": round(commit_mean("recall"), 4),
        "hit_per_commit": round(commit_mean("hit"), 4),
        "index_seconds": round(parse_s + graph_s, 2),
        "query_ms_mean": round(mean("query_ms"), 1),
        "query_ms_p95": round(
            sorted(r["query_ms"] for r in rows)[int(0.95 * (len(rows) - 1))], 1),
        "config": {
            "token_budget": TOKEN_BUDGET,
            "weights": "hybrid (LORO-validated 0.3/0.5/0.2)",
            "token_estimator": "len(source)//4",
            "denominator": "sum of indexed function bodies (excludes "
                           "module-level code, non-Python files)",
        },
    }
    print(f"  retrieved {result['retrieved_functions']} fns / "
          f"{result['retrieved_tokens']:,} tokens per query "
          f"= {result['token_reduction_pct']:.2f}% reduction")
    print(f"  precision {result['precision']:.3f}  recall {result['recall']:.3f}  "
          f"query {result['query_ms_mean']:.0f}ms")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repos", nargs="*", help="repo paths (default: all benchmark repos)")
    ap.add_argument("--commits", type=int, default=TARGET_COMMITS,
                    help="distinct commits to mine per repo")
    args = ap.parse_args()

    if args.repos:
        repo_paths = args.repos
    else:
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "benchmark_repos")
        repo_paths = [os.path.join(root, r) for r in DEFAULT_REPOS
                      if os.path.isdir(os.path.join(root, r))]

    results = []
    for path in repo_paths:
        try:
            r = measure_repo(path, target_commits=args.commits)
        except Exception as exc:                       # one bad repo != no run
            print(f"  !! {os.path.basename(path)} failed: {exc}", file=sys.stderr)
            continue
        if r:
            results.append(r)

    if not results:
        print("no repos measured", file=sys.stderr)
        return 1

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out = os.path.join(RESULTS_DIR, "reduction.json")
    with open(out, "w") as fh:
        json.dump({"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "repos": results}, fh, indent=2)
    print(f"\nwrote {out}")

    hdr = (f"\n{'repo':<11}{'fns':>7}{'repo tok':>11}{'ret fns':>9}"
           f"{'ret tok':>9}{'reduc':>8}{'prec':>7}{'rec':>7}{'ms':>7}")
    print(hdr + "\n" + "-" * len(hdr.strip()))
    for r in results:
        print(f"{r['repo']:<11}{r['total_functions']:>7,}{r['full_repo_tokens']:>11,}"
              f"{r['retrieved_functions']:>9.1f}{r['retrieved_tokens']:>9,}"
              f"{r['token_reduction_pct']:>7.1f}%{r['precision']:>7.3f}"
              f"{r['recall']:>7.3f}{r['query_ms_mean']:>7.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
