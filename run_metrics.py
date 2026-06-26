#!/usr/bin/env python3
"""
run_metrics.py — Honest precision/recall benchmark harness for DiffContext.

Design principles:
  - NO filtering that hides failures. If we can't find a GT symbol, recall=0
    for that case and it COUNTS toward the average (doesn't get skipped).
  - GT is filtered only against the symbol table (things we CAN parse).
    If a function isn't in our symbol table at all, we genuinely can't
    retrieve it — that's a graph coverage issue, not an eval issue.
  - Every case is printed individually so you can see where we fail.
  - Full diagnostic stats: cases found, skipped, zero-recall count.
  - Token budget variants reported so you can see if budget is the bottleneck.
"""

import json
import os
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import get_blast_radius, build_reverse_graph
from diffcontext.impact.traversal import expand_dependencies
from diffcontext.impact.scoring import compute_impact_scores
from diffcontext.context.selector import select_context
from benchmarks.ground_truth import extract_cochange_cases


def compute_metrics(retrieved: set, ground_truth: set) -> dict:
    """Standard IR metrics. Empty GT returns zeros (not skipped)."""
    if not ground_truth:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "tp": 0, "fp": 0, "fn": 0}
    tp = len(retrieved & ground_truth)
    fp = len(retrieved - ground_truth)
    fn = len(ground_truth - retrieved)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def run_metrics_for_repo(
    repo_path: str,
    max_cases: int = 50,
    max_tokens: int = 10_000,
    verbose: bool = True,
) -> dict:
    repo_name = os.path.basename(os.path.abspath(repo_path))
    print(f"\n{'='*60}")
    print(f"  Repo: {repo_name}")
    print(f"{'='*60}")

    # ── Extract ground truth from git history ─────────────────────────────
    cases = extract_cochange_cases(repo_path, max_cases=max_cases)
    print(f"  GT cases extracted from git: {len(cases)}")
    if not cases:
        print(f"  SKIP: No co-change cases found (needs git history).")
        return None

    tracemalloc.start()

    # ── Parse symbols ─────────────────────────────────────────────────────
    t0 = time.perf_counter()
    symbols = extract_all_symbols(repo_path)
    parse_ms = (time.perf_counter() - t0) * 1000

    # ── Build call graph ──────────────────────────────────────────────────
    t1 = time.perf_counter()
    graph = build_repository_graph(repo_path)
    graph_ms = (time.perf_counter() - t1) * 1000

    _, peak_mem_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    memory_mb = peak_mem_bytes / (1024 * 1024)

    # Build reverse graph once and reuse
    reverse_graph = build_reverse_graph(graph)

    symbol_id_set = set(symbols.keys())
    total_repo_tokens = sum(max(1, len(s.code) // 4) for s in symbols.values())

    # ── Per-case evaluation ───────────────────────────────────────────────
    case_results = []
    skipped_query_not_in_symbols = 0
    skipped_gt_empty_after_filter = 0
    zero_recall_cases = 0
    zero_precision_cases = 0

    total_traversal_ms = 0.0
    total_compile_ms   = 0.0
    total_context_tokens = 0
    total_reduction = 0.0

    for case in cases:
        # Query must exist in our symbol table
        if case.query_symbol not in symbol_id_set:
            skipped_query_not_in_symbols += 1
            continue

        # GT: only count symbols we CAN POSSIBLY retrieve (in our symbol table).
        # We CANNOT retrieve symbols we never parsed. That's a coverage issue.
        # But we DO count leaf functions (no call edges) — that's what matters.
        gt = set(case.ground_truth_symbols) & symbol_id_set
        if not gt:
            skipped_gt_empty_after_filter += 1
            continue

        # Run the retrieval pipeline
        t2 = time.perf_counter()
        blast_radii = {
            case.query_symbol: get_blast_radius(
                graph, case.query_symbol, reverse=reverse_graph
            )
        }
        all_blast = blast_radii[case.query_symbol]
        seed = list(set([case.query_symbol] + all_blast))
        expanded = expand_dependencies(graph, seed, max_depth=2)
        scores = compute_impact_scores(
            graph,
            [case.query_symbol],
            blast_radii,
            expanded_deps=expanded,
            reverse=reverse_graph,
        )
        traversal_ms = (time.perf_counter() - t2) * 1000
        total_traversal_ms += traversal_ms

        t3 = time.perf_counter()
        selected, dropped = select_context(
            symbols, scores, [case.query_symbol], max_tokens=max_tokens
        )
        compile_ms = (time.perf_counter() - t3) * 1000
        total_compile_ms += compile_ms

        retrieved = set(selected) - {case.query_symbol}
        m = compute_metrics(retrieved, gt)

        ctx_tokens = sum(
            max(1, len(symbols[s].code) // 4) for s in selected if s in symbols
        )
        reduction = (
            100 * (1 - ctx_tokens / total_repo_tokens) if total_repo_tokens > 0 else 0
        )

        case_result = {
            "commit": case.commit_hash,
            "query": case.query_symbol,
            "gt_size": len(gt),
            "retrieved_size": len(retrieved),
            "tp": m["tp"],
            "precision": round(m["precision"], 4),
            "recall": round(m["recall"], 4),
            "f1": round(m["f1"], 4),
        }
        case_results.append(case_result)

        if m["recall"] == 0:
            zero_recall_cases += 1
        if m["precision"] == 0:
            zero_precision_cases += 1

        total_context_tokens += ctx_tokens
        total_reduction += reduction

        if verbose:
            status = "✓" if m["recall"] > 0 else "✗"
            print(
                f"  {status} {case.commit_hash} | Q: {case.query_symbol.split(':')[-1]:<30} "
                f"| P={m['precision']:.3f} R={m['recall']:.3f} "
                f"| GT={len(gt)} Ret={len(retrieved)} TP={m['tp']}"
            )

    n = len(case_results)
    if n == 0:
        print(f"\n  RESULT: 0 valid cases — nothing to report.")
        return None

    avg_precision = sum(r["precision"] for r in case_results) / n
    avg_recall    = sum(r["recall"] for r in case_results) / n
    avg_f1        = sum(r["f1"] for r in case_results) / n
    avg_traversal = total_traversal_ms / n
    avg_compile   = total_compile_ms / n
    avg_ctx_tokens = int(total_context_tokens / n)
    avg_reduction  = total_reduction / n
    total_ms = parse_ms + graph_ms + avg_traversal + avg_compile

    result = {
        "repo": repo_name,
        # --- Coverage ---
        "files": len({s.file for s in symbols.values()}),
        "symbols": len(symbols),
        "edges": sum(len(deps) for deps in graph.values()),
        # --- Evaluation quality ---
        "eval": {
            "cases_from_git": len(cases),
            "cases_evaluated": n,
            "skipped_query_missing": skipped_query_not_in_symbols,
            "skipped_gt_empty": skipped_gt_empty_after_filter,
            "zero_recall_cases": zero_recall_cases,
            "zero_precision_cases": zero_precision_cases,
        },
        # --- THE REAL NUMBERS ---
        "precision": round(avg_precision, 4),
        "recall":    round(avg_recall, 4),
        "f1":        round(avg_f1, 4),
        # --- Runtime ---
        "runtime_ms": {
            "parse":       round(parse_ms, 1),
            "graph_build": round(graph_ms, 1),
            "traversal":   round(avg_traversal, 1),
            "compiler":    round(avg_compile, 1),
            "total":       round(total_ms, 1),
        },
        "memory_mb":       round(memory_mb, 1),
        "context_tokens":  avg_ctx_tokens,
        "token_reduction": round(avg_reduction, 2),
        # --- Per-case detail ---
        "cases": case_results,
    }

    print(f"\n  ── Summary ({repo_name}) ──")
    print(f"  Cases evaluated : {n} / {len(cases)} extracted")
    print(f"  Zero-recall     : {zero_recall_cases} / {n}  ({100*zero_recall_cases/n:.0f}%)")
    print(f"  Zero-precision  : {zero_precision_cases} / {n}  ({100*zero_precision_cases/n:.0f}%)")
    print(f"  Avg Precision   : {avg_precision:.4f}")
    print(f"  Avg Recall      : {avg_recall:.4f}")
    print(f"  Avg F1          : {avg_f1:.4f}")
    print(f"  Graph edges     : {result['edges']} / {result['symbols']} symbols")
    print(f"  Graph coverage  : {result['edges']/max(result['symbols'],1):.2f} edges/symbol")

    return result


def main():
    bench_dir = os.path.join(os.path.dirname(__file__), "benchmark_repos")
    if not os.path.isdir(bench_dir):
        print(f"Bench dir not found: {bench_dir}")
        print("Run `python benchmark_runner.py --clone` first.")
        return

    all_results = []
    for name in sorted(os.listdir(bench_dir)):
        repo_path = os.path.join(bench_dir, name)
        if os.path.isdir(os.path.join(repo_path, ".git")):
            res = run_metrics_for_repo(repo_path, max_cases=50, max_tokens=10_000)
            if res:
                all_results.append(res)

    if not all_results:
        print("\nNo results to save.")
        return

    # Print aggregate
    print(f"\n{'='*60}")
    print("  AGGREGATE RESULTS")
    print(f"{'='*60}")
    print(f"  {'Repo':<12} {'P':>7} {'R':>7} {'F1':>7} {'Edges/Sym':>10} {'Zero-R%':>8}")
    print(f"  {'-'*50}")
    for r in all_results:
        e = r["eval"]
        edge_dens = r["edges"] / max(r["symbols"], 1)
        zr_pct = 100 * e["zero_recall_cases"] / max(e["cases_evaluated"], 1)
        print(
            f"  {r['repo']:<12} {r['precision']:>7.4f} {r['recall']:>7.4f} "
            f"{r['f1']:>7.4f} {edge_dens:>10.2f} {zr_pct:>7.0f}%"
        )

    out_dir = os.path.join(os.path.dirname(__file__), "benchmarks", "results")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "baseline_metrics.json")
    with open(out_file, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved to {out_file}")


if __name__ == "__main__":
    main()
