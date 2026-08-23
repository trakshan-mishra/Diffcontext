#!/usr/bin/env python3
"""
ablation_selector.py — Phase 2+3: controlled ablation of selection parameters.

Indexes each task ONCE (the expensive part), then tests multiple selection
configs (the cheap part) against the same impact scores. Produces the
comparison table: recall, precision, F1, selected symbols, context tokens,
runtime per config.

Configs tested (one change at a time from baseline):
  baseline_gap      — gap cutoff, top_k=20, max_tokens=8000 (current default)
  baseline_nogap    — no cutoff, top_k=20, max_tokens=8000 (seeds_plus_retrieved)
  topk_40           — no cutoff, top_k=40, max_tokens=8000
  topk_60           — no cutoff, top_k=60, max_tokens=8000
  budget_12k        — no cutoff, top_k=20, max_tokens=12000
  budget_16k        — no cutoff, top_k=20, max_tokens=16000
  floor_30          — score-floor>=30, top_k=40, max_tokens=8000
  floor_40          — score-floor>=40, top_k=40, max_tokens=8000
  floor_30_gap      — score-floor>=30 THEN gap, top_k=40, max_tokens=8000
  combined          — floor_30, top_k=60, max_tokens=12000

The score-floor is implemented by filtering impact.scores before compile,
which is equivalent to a floor cutoff policy when cutoff=None (no gap).

Usage:
  HF_HUB_OFFLINE=1 python3 benchmarks/contextbench/ablation_selector.py
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))

from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile
from benchmarks.contextbench.run_diffcontext import (
    seed_symbols_from_patch, gold_line_spans, sym_to_span,
    spans_dict_from_syms, self_metrics,
    CANONICAL_CLONES, REPO_TO_LOCAL, Worktree,
)
from benchmarks.contextbench.analyze_retrieval_failures import (
    gold_symbols_from_context,
)


CONFIGS = [
    # (name, cutoff, top_k, max_tokens, score_floor)
    ("baseline_gap",    "gap",  20,  8000, None),
    ("baseline_nogap",  None,   20,  8000, None),
    ("topk_40",          None,   40,  8000, None),
    ("topk_60",          None,   60,  8000, None),
    ("budget_12k",       None,   20, 12000, None),
    ("budget_16k",       None,   20, 16000, None),
    ("floor_30",         None,   40,  8000, 30.0),
    ("floor_40",         None,   40,  8000, 40.0),
    ("floor_30_gap",    "gap",   40,  8000, 30.0),
    ("combined",        None,   60, 12000, 30.0),
]


def run_config(index, impact, seeds, cutoff, top_k, max_tokens, score_floor):
    """Run one selection config. Returns (selected_sids, ctx_tokens, elapsed)."""
    t0 = time.perf_counter()
    # Apply score-floor pre-filter (equivalent to a floor cutoff when cutoff=None)
    if score_floor is not None:
        filtered_scores = {sid: sc for sid, sc in impact.scores.items()
                           if sc >= score_floor or sid in set(seeds)}
        # Build a modified ImpactResult with filtered scores
        from diffcontext.models import ImpactResult
        impact_filtered = ImpactResult(
            changed=impact.changed,
            blast_radius=impact.blast_radius,
            dependencies=impact.dependencies,
            scores=filtered_scores,
        )
        pkg = dc_compile(index, impact_filtered, max_tokens=max_tokens,
                         top_k=top_k, cutoff=cutoff)
    else:
        pkg = dc_compile(index, impact, max_tokens=max_tokens,
                         top_k=top_k, cutoff=cutoff)
    elapsed = time.perf_counter() - t0
    selected = [it.symbol_id for it in (pkg.items or [])]
    ctx_tokens = getattr(pkg, "token_estimate", 0)
    return selected, ctx_tokens, elapsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir",
                    default=os.path.join(_HERE, "results", "ablation_selector"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--repos", default="")
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

    # Per-config results: {config_name: [per_task_metrics, ...]}
    config_results: Dict[str, List[dict]] = {name: [] for name, *_ in CONFIGS}
    config_selected: Dict[str, Dict[str, List[str]]] = {name: {} for name, *_ in CONFIGS}
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

                # Expensive: index + analyze_impact (done ONCE per task)
                impact = analyze_impact(index, seeds, hybrid=True, adaptive=True)

                # Cheap: test all configs against the same impact
                for cname, cutoff, top_k, max_tokens, floor in CONFIGS:
                    selected, ctx_tokens, elapsed = run_config(
                        index, impact, seeds, cutoff, top_k, max_tokens, floor,
                    )
                    # Compute retrieval metrics
                    all_sids = seeds + [s for s in selected if s not in set(seeds)]
                    spans = spans_dict_from_syms(index, all_sids)
                    spans_dict = {f: [(s["start"], s["end"]) for s in ivs]
                                  for f, ivs in spans.items()}
                    metrics = self_metrics(
                        {f: [{"start": s["start"], "end": s["end"]} for s in ivs]
                         for f, ivs in spans.items()},
                        row["gold_context"],
                    )
                    # Symbol-level recall: how many gold symbols are covered?
                    covered = set(seeds) | set(selected)
                    gold_covered = sum(1 for g in gold_sids if g in covered)
                    sym_recall = gold_covered / len(gold_sids) if gold_sids else 1.0

                    config_results[cname].append({
                        "instance_id": iid,
                        "n_selected": len(selected),
                        "ctx_tokens": ctx_tokens,
                        "sec": round(elapsed, 4),
                        "line_recall": metrics["line_recall"],
                        "line_precision": metrics["line_precision"],
                        "line_f1": metrics["line_f1"],
                        "sym_recall": round(sym_recall, 4),
                        "gold_covered": gold_covered,
                        "gold_total": len(gold_sids),
                    })
                    config_selected[cname][iid] = selected

                if task_n % 10 == 0:
                    print(f"  ...{task_n} tasks done")
                count += 1
            except Exception as e:
                print(f"[{local_repo}] {iid} ERROR {type(e).__name__}: {e}")
                count += 1

    if wt is not None:
        wt.remove()

    # ── Summary table ─────────────────────────────────────────────────────
    import statistics as st
    print("\n" + "=" * 90)
    print(f"PHASE 2+3 — SELECTION ABLATION (n={task_n} tasks)")
    print("=" * 90)
    print(f"\n{'config':<18} {'line_rec':>8} {'line_prec':>9} {'line_f1':>8} "
          f"{'sym_rec':>8} {'n_sel':>6} {'tokens':>7} {'sec':>6}")
    print("-" * 80)
    for cname, *_ in CONFIGS:
        results = config_results[cname]
        if not results:
            continue
        lr = st.mean(r["line_recall"] for r in results)
        lp = st.mean(r["line_precision"] for r in results)
        lf = st.mean(r["line_f1"] for r in results)
        sr = st.mean(r["sym_recall"] for r in results)
        ns = st.mean(r["n_selected"] for r in results)
        tk = st.mean(r["ctx_tokens"] for r in results)
        sc = st.mean(r["sec"] for r in results)
        print(f"{cname:<18} {lr:>8.3f} {lp:>9.3f} {lf:>8.3f} "
              f"{sr:>8.3f} {ns:>6.1f} {tk:>7.0f} {sc:>6.3f}")

    # Write JSON
    out_path = os.path.join(args.out_dir, "ablation.json")
    with open(out_path, "w") as f:
        json.dump({
            "n_tasks": task_n,
            "configs": {name: {"params": {"cutoff": c, "top_k": k, "max_tokens": t, "score_floor": f},
                              "results": config_results[name]}
                        for name, c, k, t, f in CONFIGS},
        }, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
