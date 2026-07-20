#!/usr/bin/env python3
"""
gt_validity.py — Measures (rather than merely discloses) how incomplete the
co-change ground truth is.

The threat: eval_v2 treats "co-changed in this commit" as the complete
relevance set. But real changes are often split across commits — the symbol
the tool retrieved and got penalized for may be changed in the very next
commit. If that happens a lot, measured precision systematically understates
true relevance, and "precision is the product's real problem" needs
re-phrasing.

The measurement: for each mined co-change commit and the product-hybrid
retrieval, take every FALSE POSITIVE (retrieved, not in that commit's GT)
and ask: was this symbol modified within the next W commits after the mined
commit? Report that rate against a size-matched RANDOM control (random
symbols hit follow-up windows too — busy repos change constantly; only the
margin over random is evidence of GT incompleteness).

Outputs per repo, for W in WINDOWS:
  fp_future_rate      — P(false positive is changed within next W commits)
  random_future_rate  — same for size-matched random draws (control)
  lift                — fp rate / random rate
  adjusted_precision  — precision if future-co-changed FPs were counted TP
  raw_precision       — for comparison

Usage:
  python benchmarks/gt_validity.py                       # default repos
  python benchmarks/gt_validity.py --repos click django  # subset
"""

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import build_reverse_graph

from benchmarks.baselines import BM25Baseline
from benchmarks.eval_v1 import truncate_by_token_budget, CANDIDATE_LIMIT
from benchmarks.eval_v2_hardened import (
    extract_distinct_commits, _hybrid_ranked,
)
from benchmarks.ground_truth import _get_changed_line_ranges, _find_functions_at_lines

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "results", "gt_validity")
DEFAULT_REPOS = ["click", "django", "flask", "httpx", "pydantic"]
WINDOWS = [3, 10]
SEED = 42


def full_commit_order(repo_path: str) -> List[str]:
    """All commit hashes, newest first (index 0 = newest)."""
    r = subprocess.run(["git", "log", "--format=%H", "--no-merges"],
                       cwd=repo_path, capture_output=True, text=True, timeout=120)
    return r.stdout.split()


def changed_symbols_at(repo_path: str, commit_hash: str,
                       cache: Dict[str, Set[str]]) -> Set[str]:
    """Symbol IDs modified in one commit (same attribution as the miner)."""
    if commit_hash in cache:
        return cache[commit_hash]
    out: Set[str] = set()
    try:
        files_res = subprocess.run(
            ["git", "diff", "--name-only", "--relative", "--diff-filter=M",
             f"{commit_hash}~1", commit_hash],
            cwd=repo_path, capture_output=True, text=True, timeout=10)
        if files_res.returncode == 0:
            for f in files_res.stdout.strip().split("\n"):
                if not f.endswith(".py") or not f.strip():
                    continue
                lines = _get_changed_line_ranges(repo_path, commit_hash, f)
                if lines:
                    out.update(_find_functions_at_lines(f, lines, repo_path,
                                                        commit_hash))
    except subprocess.TimeoutExpired:
        pass
    cache[commit_hash] = out
    return out


def future_changed(repo_path: str, order: List[str], pos: int, window: int,
                   cache: Dict[str, Set[str]]) -> Set[str]:
    """Symbols changed in the `window` commits AFTER (newer than) order[pos]."""
    out: Set[str] = set()
    for j in range(max(0, pos - window), pos):
        out |= changed_symbols_at(repo_path, order[j], cache)
    return out


def eval_repo(repo_path: str) -> Optional[Dict]:
    name = os.path.basename(os.path.abspath(repo_path))
    t0 = time.perf_counter()
    commits = extract_distinct_commits(repo_path)
    symbols = extract_all_symbols(repo_path)
    graph = build_repository_graph(repo_path)
    reverse_graph = build_reverse_graph(graph)
    sset = set(symbols.keys())
    symbol_ids = list(symbols.keys())
    bm25 = BM25Baseline(symbols)
    order = full_commit_order(repo_path)
    pos_of = {h[:10]: i for i, h in enumerate(order)}
    print(f"  [{name}] {len(commits)} commits mined, {len(symbols)} symbols, "
          f"history {len(order)} commits ({time.perf_counter()-t0:.0f}s)", flush=True)

    attr_cache: Dict[str, Set[str]] = {}
    rng = random.Random(SEED)
    stats = {w: {"fp_hits": 0, "fp_total": 0, "rand_hits": 0, "rand_total": 0,
                 "tp": 0, "retrieved": 0}
             for w in WINDOWS}
    n_queries = 0

    for c in commits:
        vs = [s for s in c.symbols if s in sset]
        if len(vs) < 2 or c.flagged_noisy:
            continue
        pos = pos_of.get(c.commit_hash)
        if pos is None or pos < max(WINDOWS):
            # too close to HEAD: the follow-up window would be truncated
            continue
        futures = {w: future_changed(repo_path, order, pos, w, attr_cache)
                   for w in WINDOWS}
        for q in vs:
            gt = set(vs) - {q}
            ranked = _hybrid_ranked(q, symbols, symbol_ids, graph, reverse_graph, bm25)
            rset = set(ranked)
            fps = rset - gt
            tps = rset & gt
            n_queries += 1
            for w in WINDOWS:
                fut = futures[w]
                stats[w]["fp_total"] += len(fps)
                stats[w]["fp_hits"] += sum(1 for s in fps if s in fut)
                stats[w]["tp"] += len(tps)
                stats[w]["retrieved"] += len(rset)
                # size-matched random control (excludes query + GT)
                pool = [s for s in symbol_ids if s != q and s not in gt]
                draw = rng.sample(pool, min(len(fps), len(pool)))
                stats[w]["rand_total"] += len(draw)
                stats[w]["rand_hits"] += sum(1 for s in draw if s in fut)

    if n_queries == 0:
        return None
    out = {"repo": name, "n_queries": n_queries,
           "head_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path,
                                      capture_output=True, text=True).stdout.strip(),
           "windows": {}}
    for w in WINDOWS:
        s = stats[w]
        fp_rate = s["fp_hits"] / s["fp_total"] if s["fp_total"] else 0.0
        rd_rate = s["rand_hits"] / s["rand_total"] if s["rand_total"] else 0.0
        raw_p = s["tp"] / s["retrieved"] if s["retrieved"] else 0.0
        adj_p = (s["tp"] + s["fp_hits"]) / s["retrieved"] if s["retrieved"] else 0.0
        out["windows"][w] = {
            "fp_future_rate": round(fp_rate, 4),
            "random_future_rate": round(rd_rate, 4),
            "lift": round(fp_rate / rd_rate, 2) if rd_rate else None,
            "raw_precision": round(raw_p, 4),
            "adjusted_precision": round(adj_p, 4),
            "n_fp": s["fp_total"],
        }
        print(f"    W={w}: FP-future {fp_rate:.3f} vs random {rd_rate:.3f} "
              f"(lift {out['windows'][w]['lift']}) | precision raw {raw_p:.3f} "
              f"-> adjusted {adj_p:.3f}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", nargs="*", default=None)
    args = ap.parse_args()
    bench = os.path.normpath(os.path.join(os.path.dirname(__file__), "..",
                                          "benchmark_repos"))
    os.makedirs(RESULTS_DIR, exist_ok=True)
    repos = args.repos if args.repos else DEFAULT_REPOS
    results = []
    for name in repos:
        path = os.path.join(bench, name)
        if not os.path.isdir(path):
            print(f"  !! missing {path}, skipping")
            continue
        print(f"[{name}]", flush=True)
        r = eval_repo(path)
        if r:
            results.append(r)
    out = os.path.join(RESULTS_DIR, "gt_validity.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
