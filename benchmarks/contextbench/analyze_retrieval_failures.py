#!/usr/bin/env python3
"""
analyze_retrieval_failures.py — Phase 2: classify WHY DiffContext misses gold
context (false-negative analysis on the 128-task effective set).

For each task with seeds (oracle localization):
  1. checkout base_commit, index
  2. extract seeds from gold patch (oracle)
  3. extract gold symbols from gold_context (human-annotated)
  4. run analyze_impact → get full impact scores
  5. run compile (default + gap) → get selected symbols
  6. compute missed gold symbols = gold_symbols - (seeds + selected)
  7. for each missed gold symbol, classify WHY:
     - reached_but_cut:  in impact.scores but cut by budget/top_k/gap
     - never_reached:    not in impact.scores — no retrieval signal reached it
       sub-classified by structural relationship to seeds:
       - same_file:        same file as a seed (window/same-dir edge missed it)
       - same_class:       same file:class as a seed
       - called_in_seed:   bare name appears in a seed's code but no graph edge
                           (resolution failure: import / attribute / inheritance
                           / dynamic dispatch)
       - inheritance:      missed symbol's class is a parent/child of a seed's
       - import_same_mod:  missed symbol's file is imported by a seed's file
       - no_structural:    none of the above — purely semantic relationship
     - not_a_symbol:     gold lines don't overlap any extracted symbol
                          (class/module-level code, not a function/method)

Output: a JSON summary + per-task detail to results/retrieval_failures/.

Usage:
  HF_HUB_OFFLINE=1 python3 benchmarks/contextbench/analyze_retrieval_failures.py
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))

from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile
from benchmarks.contextbench.run_diffcontext import (
    seed_symbols_from_patch, gold_line_spans, sym_to_span,
    CANONICAL_CLONES, REPO_TO_LOCAL, Worktree,
)


def gold_symbols_from_context(index, gold_context: str) -> Tuple[List[str], List[dict]]:
    """Map gold_context line ranges to symbol IDs. Returns (gold_sids, misses)
    where misses = gold line ranges that don't overlap any symbol."""
    gold_spans = gold_line_spans(gold_context)
    if not gold_spans:
        return [], []
    by_file: Dict[str, List[Tuple[str, int, int]]] = {}
    for sid, sym in index.symbols.items():
        rel = sid.split(":", 1)[0]
        if rel.startswith("./"):
            rel = rel[2:]
        end = sym.lineno + max(len(sym.code.splitlines()) - 1, 0)
        by_file.setdefault(rel, []).append((sid, sym.lineno, end))
    gold_sids: List[str] = []
    misses: List[dict] = []
    for f, ranges in gold_spans.items():
        for rlo, rhi in ranges:
            hit = False
            for sid, lo, end in by_file.get(f, []):
                if not (end < rlo or lo > rhi):
                    gold_sids.append(sid)
                    hit = True
            if not hit:
                misses.append({"file": f, "start": rlo, "end": rhi})
    seen, out = set(), []
    for s in gold_sids:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out, misses


def _sym_file(sid: str) -> str:
    f = sid.split(":", 1)[0]
    return f[2:] if f.startswith("./") else f


def _sym_class(sid: str) -> Optional[str]:
    name = sid.split(":", 1)[1] if ":" in sid else ""
    return name.split(".", 1)[0] if "." in name else None


def _sym_name(sid: str) -> str:
    return sid.split(":", 1)[1] if ":" in sid else sid


def _bfs_reachable(graph: Dict[str, List[str]], seeds: List[str]) -> Set[str]:
    """All symbols reachable from seeds via forward+reverse graph."""
    visited = set()
    stack = list(seeds)
    while stack:
        n = stack.pop()
        if n in visited:
            continue
        visited.add(n)
        for d in graph.get(n, []):
            if d not in visited:
                stack.append(d)
    # also reverse
    rev: Dict[str, Set[str]] = {}
    for caller, callees in graph.items():
        for callee in callees:
            rev.setdefault(callee, set()).add(caller)
    stack = list(seeds)
    rev_visited = set()
    while stack:
        n = stack.pop()
        if n in rev_visited:
            continue
        rev_visited.add(n)
        for c in rev.get(n, set()):
            if c not in rev_visited:
                stack.append(c)
    return visited | rev_visited


def _name_in_code(name: str, code: str) -> bool:
    """Check if a bare name appears as a word in seed code (not just a
    substring). Uses word-boundary regex to avoid false positives."""
    return bool(re.search(r"\b" + re.escape(name) + r"\b", code))


def classify_missed(sid: str, seeds: List[str], index, reachable: Set[str],
                    seed_files: Set[str], seed_classes: Set[str],
                    seed_code: str, import_maps: Optional[Dict]) -> str:
    """Classify WHY a gold symbol was missed. Returns one of:
    reached_but_cut, same_file, same_class, called_in_seed, inheritance,
    import_same_mod, no_structural.
    """
    if sid in reachable:
        return "reached_but_cut"
    f = _sym_file(sid)
    cls = _sym_class(sid)
    name = _sym_name(sid)
    # same file?
    if f in seed_files:
        return "same_file"
    # same class (different file, same class name)?
    if cls and cls in seed_classes:
        return "same_class"
    # called/referenced in seed code but no graph edge?
    if _name_in_code(name, seed_code) or (cls and _name_in_code(cls, seed_code)):
        return "called_in_seed"
    # inheritance: missed symbol's class is parent/child of a seed's class?
    if cls:
        for sid2 in seeds:
            cls2 = _sym_class(sid2)
            if cls2 and cls != cls2:
                # check if cls inherits from cls2 or vice versa via the graph's
                # inheritance structures — approximate via code text
                sym = index.symbols.get(sid)
                if sym and ("class " + cls) in sym.code and cls2 in sym.code:
                    return "inheritance"
                sym2 = index.symbols.get(sid2)
                if sym2 and ("class " + cls2) in sym2.code and cls in sym2.code:
                    return "inheritance"
    # import: missed symbol's file is imported by a seed's file?
    if import_maps:
        for sf in seed_files:
            imap = import_maps.get("./" + sf, {})
            for _local, abs_path in imap.items():
                imp_rel = os.path.relpath(abs_path, index._repo_path or "")
                if imp_rel == f:
                    return "import_same_mod"
    return "no_structural"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir",
                    default=os.path.join(_HERE, "results", "retrieval_failures"))
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--repos", default="")
    ap.add_argument("--variant", default="diffcontext_gap",
                    choices=["diffcontext_gap", "seeds_plus_retrieved"],
                    help="Which variant's selection to analyze")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

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

    cutoff = "gap" if args.variant == "diffcontext_gap" else None
    per_task: List[dict] = []
    overall_counts: Counter = Counter()
    overall_gold_sym_counts: Counter = Counter()  # total gold symbols
    overall_seed_counts: Counter = Counter()
    overall_missed_counts: Counter = Counter()
    overall_not_symbol: int = 0
    wt: Optional[Worktree] = None
    task_n = 0

    for local_repo, rows in by_repo.items():
        repo_path = CANONICAL_CLONES.get(local_repo,
                                         os.path.join(_HERE, "..", "..",
                                                      "benchmark_repos", local_repo))
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            print(f"[skip] {local_repo}: no clone at {repo_path}")
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
                    continue  # oracle miss — skip
                gold_sids, gold_misses = gold_symbols_from_context(index, row["gold_context"])
                task_n += 1

                # Run retrieval
                impact = analyze_impact(index, seeds, hybrid=True, adaptive=True)
                pkg = dc_compile(index, impact, max_tokens=args.max_tokens,
                                 top_k=args.top_k, cutoff=cutoff)
                selected = set(it.symbol_id for it in (pkg.items or []))

                # Missed gold symbols
                seed_set = set(seeds)
                covered = seed_set | selected
                missed = [s for s in gold_sids if s not in covered]
                not_symbol = len(gold_misses)  # gold lines with no symbol

                # Classify each missed symbol
                reachable = _bfs_reachable(index.graph, seeds)
                seed_files = set(_sym_file(s) for s in seeds)
                seed_classes = set(_sym_class(s) for s in seeds if _sym_class(s))
                seed_code = "\n".join(index.symbols[s].code for s in seeds if s in index.symbols)
                import_maps = getattr(index, "_import_maps", None)

                task_classes: Counter = Counter()
                missed_details: List[dict] = []
                for sid in missed:
                    if sid not in index.symbols:
                        task_classes["not_in_index"] += 1
                        continue
                    cls = classify_missed(sid, seeds, index, reachable,
                                           seed_files, seed_classes, seed_code,
                                           import_maps)
                    task_classes[cls] += 1
                    sym = index.symbols[sid]
                    missed_details.append({
                        "sid": sid, "classification": cls,
                        "file": _sym_file(sid),
                        "name": _sym_name(sid),
                        "lineno": sym.lineno,
                        "in_scores": sid in impact.scores,
                        "score": round(impact.scores.get(sid, 0.0), 2),
                    })

                # Aggregate
                overall_counts.update(task_classes)
                overall_gold_sym_counts[local_repo] += len(gold_sids)
                overall_seed_counts[local_repo] += len(seeds)
                overall_missed_counts[local_repo] += len(missed)
                overall_not_symbol += not_symbol

                per_task.append({
                    "instance_id": iid, "repo": local_repo,
                    "n_seeds": len(seeds), "n_gold_syms": len(gold_sids),
                    "n_selected": len(selected), "n_missed": len(missed),
                    "n_not_symbol": not_symbol,
                    "classifications": dict(task_classes),
                    "missed_details": missed_details,
                })

                if task_classes:
                    print(f"[{local_repo}] {iid[-12:]} "
                          f"gold={len(gold_sids)} seeds={len(seeds)} "
                          f"sel={len(selected)} missed={len(missed)} "
                          f"not_sym={not_symbol} "
                          f"{dict(task_classes)}")
                count += 1
            except Exception as e:
                print(f"[{local_repo}] {iid} ERROR {type(e).__name__}: {e}")
                count += 1

    if wt is not None:
        wt.remove()

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print(f"PHASE 2 — RETRIEVAL FALSE-NEGATIVE ANALYSIS (variant={args.variant})")
    print(f"Tasks analyzed: {task_n} (with seeds, effective set)")
    print("=" * 74)

    total_gold = sum(overall_gold_sym_counts.values())
    total_seeds = sum(overall_seed_counts.values())
    total_missed = sum(overall_missed_counts.values())
    covered = total_gold - total_missed
    print(f"\nGold symbols: {total_gold}  Seeds: {total_seeds}  "
          f"Selected+seeds cover: {covered}  Missed: {total_missed}  "
          f"({total_missed / total_gold:.0%} of gold missed)")
    print(f"Gold lines with no symbol (class/module-level): {overall_not_symbol}")

    print(f"\n## Missed-symbol classification (n={sum(overall_counts.values())})")
    for cls, n in overall_counts.most_common():
        print(f"  {cls:<20} {n:>4}  ({n / sum(overall_counts.values()):.0%})")

    # per-repo
    print(f"\n## Per-repo coverage")
    for repo in sorted(overall_gold_sym_counts):
        g = overall_gold_sym_counts[repo]
        m = overall_missed_counts[repo]
        print(f"  {repo:<12} gold={g:>4} missed={m:>4} coverage={1 - m / g:.0%}")

    # Write JSON
    out_path = os.path.join(args.out_dir, "failures.json")
    with open(out_path, "w") as f:
        json.dump({
            "variant": args.variant,
            "n_tasks": task_n,
            "total_gold_syms": total_gold,
            "total_seeds": total_seeds,
            "total_missed": total_missed,
            "total_not_symbol": overall_not_symbol,
            "classifications": dict(overall_counts),
            "per_repo": {repo: {"gold": overall_gold_sym_counts[repo],
                               "missed": overall_missed_counts[repo]}
                        for repo in overall_gold_sym_counts},
            "per_task": per_task,
        }, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
