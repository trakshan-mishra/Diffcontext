#!/usr/bin/env python3
"""
diagnose_selector.py — Phase 1: instrument the selector to expose WHY each
reached symbol gets cut. Replays the exact selection logic from
select_context() and records the cut reason for every scored symbol.

For each task with seeds:
  1. checkout, index, extract seeds, run analyze_impact
  2. replay selection with diagnosis: for every scored symbol, record
     score, rank, dependency type, graph depth, selected/cut, and cut reason:
       gap_cut       — cut by largest-gap policy (cutoff="gap")
       top_k_cut     — ranked beyond top_k (default 20)
       per_sym_cap   — rendered size > 25% of budget
       budget_cut    — would exceed max_tokens
       selected      — included in context
  3. join with gold symbols to classify the 422 reached_but_cut cases

Output: results/selector_diagnosis/diagnosis.json + summary to stdout.

Usage:
  HF_HUB_OFFLINE=1 python3 benchmarks/contextbench/diagnose_selector.py
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))

from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile
from diffcontext.context.selector import (
    gap_cut_count, GAP_CUTOFF_WINDOW, GAP_SCORE_EPSILON, MAX_SINGLE_SYMBOL_FRACTION
)
from diffcontext.context.compiler import build_reverse_graph, relationship_cap, render_symbol_block
from benchmarks.contextbench.run_diffcontext import (
    seed_symbols_from_patch, gold_line_spans,
    CANONICAL_CLONES, REPO_TO_LOCAL, Worktree,
)
from benchmarks.contextbench.analyze_retrieval_failures import (
    gold_symbols_from_context, _sym_file, _sym_class, _sym_name,
)


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4 * 1.2))


def diagnose_selection(
    index,
    impact,
    seeds: List[str],
    max_tokens: int = 8000,
    top_k: int = 20,
    cutoff: Optional[str] = "gap",
) -> List[dict]:
    """Replay select_context with per-symbol diagnosis. Returns a list of
    {sid, score, rank, dep_type, depth, selected, cut_reason, rendered_tokens}."""
    symbols = index.symbols
    scores = impact.scores
    changed = seeds
    changed_set = set(changed)
    graph = index.graph
    reverse = index.reverse_graph
    rel_cap = relationship_cap(max_tokens)
    count = _estimate_tokens

    def rendered_size(sym_id: str) -> int:
        return count(render_symbol_block(
            sym_id, symbols, scores.get(sym_id, 0), graph, reverse,
            set(), rel_cap=rel_cap,
        ))

    per_sym_cap = int(max_tokens * MAX_SINGLE_SYMBOL_FRACTION)

    # Scored non-changed symbols, ranked by score descending
    scored = sorted(
        ((sid, sc) for sid, sc in scores.items() if sid not in changed_set),
        key=lambda x: x[1], reverse=True,
    )

    # Gap cutoff
    gap_kept: Optional[Set[str]] = None
    if cutoff == "gap":
        candidates = [(sid, sc) for sid, sc in scored
                      if sid in symbols and sc > GAP_SCORE_EPSILON]
        keep_n = gap_cut_count([sc for _, sc in candidates])
        gap_kept = {sid for sid, _ in candidates[:keep_n]}

    # Replay selection, tracking budget and cut reasons
    current_tokens = 0
    for sym_id in changed:
        if sym_id in symbols:
            current_tokens += rendered_size(sym_id)

    results: List[dict] = []
    included_non_changed = 0
    for rank, (sym_id, score) in enumerate(scored):
        if sym_id not in symbols:
            continue

        entry = {
            "sid": sym_id,
            "score": round(score, 2),
            "rank": rank,
            "selected": False,
            "cut_reason": None,
        }

        if gap_kept is not None and sym_id not in gap_kept:
            entry["cut_reason"] = "gap_cut"
            results.append(entry)
            continue

        if top_k is not None and included_non_changed >= top_k:
            entry["cut_reason"] = "top_k_cut"
            results.append(entry)
            continue

        sym_tokens = rendered_size(sym_id)
        entry["rendered_tokens"] = sym_tokens

        if per_sym_cap is not None and sym_tokens > per_sym_cap:
            entry["cut_reason"] = "per_sym_cap"
            results.append(entry)
            continue

        if max_tokens is not None and current_tokens + sym_tokens > max_tokens:
            entry["cut_reason"] = "budget_cut"
            results.append(entry)
            continue

        entry["selected"] = True
        entry["cut_reason"] = "selected"
        results.append(entry)
        included_non_changed += 1
        current_tokens += sym_tokens

    return results


def _dep_type(sid: str, seeds: List[str], index) -> str:
    """Classify a symbol's relationship to the seeds: changed, callee,
    caller, sibling, blast, or none."""
    if sid in set(seeds):
        return "changed"
    graph = index.graph
    reverse = index.reverse_graph
    seed_set = set(seeds)
    # direct callee of a seed?
    for s in seeds:
        if sid in graph.get(s, []):
            return "callee"
    # direct caller of a seed?
    for s in seeds:
        if sid in reverse.get(s, set()):
            return "caller"
    # sibling (shares a caller with a seed)?
    for s in seeds:
        for caller in reverse.get(s, set()):
            if sid in graph.get(caller, []):
                return "sibling"
    # in blast radius (reachable from seeds in reverse graph)?
    for s in seeds:
        if sid in reverse.get(s, set()):
            return "caller"
    return "other"


def _graph_depth(sid: str, seeds: List[str], graph, reverse) -> int:
    """BFS depth from the nearest seed (forward+reverse). 0=seed, 1=direct."""
    seed_set = set(seeds)
    if sid in seed_set:
        return 0
    visited = set(seeds)
    # forward
    frontier = list(seeds)
    for depth in range(1, 9):
        next_f = []
        for n in frontier:
            for d in graph.get(n, []):
                if d == sid:
                    return depth
                if d not in visited:
                    visited.add(d)
                    next_f.append(d)
        frontier = next_f
    # reverse
    visited = set(seeds)
    frontier = list(seeds)
    for depth in range(1, 9):
        next_f = []
        for n in frontier:
            for c in reverse.get(n, set()):
                if c == sid:
                    return depth
                if c not in visited:
                    visited.add(c)
                    next_f.append(c)
        frontier = next_f
    return -1  # unreachable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir",
                    default=os.path.join(_HERE, "results", "selector_diagnosis"))
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--repos", default="")
    ap.add_argument("--variant", default="diffcontext_gap",
                    choices=["diffcontext_gap", "seeds_plus_retrieved"])
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    cutoff = "gap" if args.variant == "diffcontext_gap" else None
    wanted = set(args.repos.split(",")) if args.repos else set(REPO_TO_LOCAL.values())

    from datasets import load_dataset
    ds = load_dataset("Contextbench/ContextBench", "default", split="train",
                      revision="c2855792b006af41c67202d33883fb9d46362853")
    tasks = [r for r in ds
             if r["language"] == "python" and r["repo"] in REPO_TO_LOCAL
             and REPO_TO_LOCAL[r["repo"]] in wanted]
    by_repo: Dict[str, List[dict]] = {}
    for r in tasks:
        by_repo.setdefault(REPO_TO_LOCAL[r["repo"]], []).append(r)
    for rep in by_repo:
        by_repo[rep].sort(key=lambda r: r["instance_id"])

    per_task: List[dict] = []
    cut_reason_counts: Counter = Counter()
    gold_cut_reasons: Counter = Counter()  # cut reason for missed GOLD symbols
    gold_dep_types: Counter = Counter()
    wt: Optional[Worktree] = None
    task_n = 0

    for local_repo, rows in by_repo.items():
        repo_path = CANONICAL_CLONES.get(local_repo,
                                         os.path.join(_HERE, "..", "..",
                                                      "benchmark_repos", local_repo))
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            print(f"[skip] {local_repo}: no clone")
            continue
        count = 0
        for row in rows:
            if args.limit and count >= args.limit:
                break
            iid = row["instance_id"]
            commit = row["base_commit"]
            try:
                if wt is None or wt.repo != os.path.abspath(repo_path):
                    if wt is not None:
                        wt.remove()
                    wt = Worktree(repo_path,
                                  os.path.abspath(os.path.join(args.out_dir, f"wt_{local_repo}")),
                                  commit)
                else:
                    wt.checkout(commit)
                index = index_repository(wt.path)
                seeds = seed_symbols_from_patch(index, row["patch"])
                if not seeds:
                    count += 1
                    continue
                gold_sids, _ = gold_symbols_from_context(index, row["gold_context"])
                task_n += 1

                impact = analyze_impact(index, seeds, hybrid=True, adaptive=True)
                diagnosis = diagnose_selection(
                    index, impact, seeds,
                    max_tokens=args.max_tokens, top_k=args.top_k, cutoff=cutoff,
                )

                # Build lookup: sid -> diagnosis entry
                diag_map = {d["sid"]: d for d in diagnosis}
                selected_set = set(d["sid"] for d in diagnosis if d["selected"])
                seed_set = set(seeds)
                covered = seed_set | selected_set

                # For each missed gold symbol, record cut reason + dep type
                task_gold_cuts: Counter = Counter()
                for gs in gold_sids:
                    if gs in covered:
                        continue  # not missed
                    if gs in seed_set:
                        continue
                    d = diag_map.get(gs)
                    if d is None:
                        task_gold_cuts["never_scored"] += 1
                        gold_cut_reasons["never_scored"] += 1
                        continue
                    reason = d.get("cut_reason", "unknown")
                    task_gold_cuts[reason] += 1
                    gold_cut_reasons[reason] += 1
                    dt = _dep_type(gs, seeds, index)
                    gold_dep_types[dt] += 1

                # Overall cut reasons (all scored symbols, not just gold)
                for d in diagnosis:
                    if not d["selected"]:
                        cut_reason_counts[d["cut_reason"]] += 1

                per_task.append({
                    "instance_id": iid, "repo": local_repo,
                    "n_seeds": len(seeds), "n_gold": len(gold_sids),
                    "n_selected": len(selected_set),
                    "n_missed_gold": len(gold_sids) - len(set(gold_sids) & covered),
                    "gold_cut_reasons": dict(task_gold_cuts),
                })

                if task_gold_cuts:
                    print(f"[{local_repo}] {iid[-12:]} "
                          f"gold={len(gold_sids)} sel={len(selected_set)} "
                          f"missed_cuts={dict(task_gold_cuts)}")
                count += 1
            except Exception as e:
                print(f"[{local_repo}] {iid} ERROR {type(e).__name__}: {e}")
                count += 1

    if wt is not None:
        wt.remove()

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print(f"PHASE 1 — SELECTOR DIAGNOSIS (variant={args.variant}, "
          f"top_k={args.top_k}, max_tokens={args.max_tokens})")
    print(f"Tasks: {task_n}")
    print("=" * 74)

    print(f"\n## Cut reasons for ALL scored-but-not-selected symbols:")
    total_cut = sum(cut_reason_counts.values())
    for reason, n in cut_reason_counts.most_common():
        print(f"  {reason:<16} {n:>6}  ({n/total_cut:.0%})" if total_cut else "")

    print(f"\n## Cut reasons for MISSED GOLD symbols (the 422 reached_but_cut):")
    total_gold_cut = sum(gold_cut_reasons.values())
    for reason, n in gold_cut_reasons.most_common():
        print(f"  {reason:<16} {n:>6}  ({n/total_gold_cut:.0%})" if total_gold_cut else "")

    print(f"\n## Dependency types of missed gold symbols:")
    for dt, n in gold_dep_types.most_common():
        print(f"  {dt:<16} {n:>6}")

    # Score distribution of gap-cut gold symbols
    gap_cut_gold = []
    for task in per_task:
        pass  # would need the detailed diagnosis; collect from a second pass
    out_path = os.path.join(args.out_dir, "diagnosis.json")
    with open(out_path, "w") as f:
        json.dump({
            "variant": args.variant,
            "top_k": args.top_k,
            "max_tokens": args.max_tokens,
            "n_tasks": task_n,
            "cut_reasons_all": dict(cut_reason_counts),
            "cut_reasons_gold": dict(gold_cut_reasons),
            "dep_types_gold": dict(gold_dep_types),
            "per_task": per_task,
        }, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
