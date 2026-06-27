#!/usr/bin/env python3
"""
diagnose_graph_gaps.py — Understand WHY the graph misses so many edges.

For each ground-truth case where the graph fails, this script answers:
  1. Is the query symbol isolated (0 edges)?
  2. Is the GT symbol isolated?
  3. What KIND of relationship exists between query and GT?
     - Same file, same class?
     - Same file, different class?
     - Different file, same package?
     - Different file, different package?
  4. What edges does the graph HAVE for the query?
  5. What edges SHOULD exist but don't? (manual heuristic analysis)

This gives us the exact list of edge types we need to add.
"""

import os
import sys
import ast
from collections import defaultdict, Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import build_reverse_graph
from benchmarks.ground_truth import extract_cochange_cases


def diagnose(repo_path: str):
    repo_path = os.path.abspath(repo_path)
    repo_name = os.path.basename(repo_path)

    print(f"\n{'='*70}")
    print(f"  GRAPH GAP DIAGNOSIS: {repo_name}")
    print(f"{'='*70}")

    # Extract ground truth
    cases = extract_cochange_cases(repo_path, max_cases=50)
    if not cases:
        print("  No co-change cases found.")
        return

    # Build infrastructure
    symbols = extract_all_symbols(repo_path)
    graph = build_repository_graph(repo_path)
    reverse_graph = build_reverse_graph(graph)
    symbol_id_set = set(symbols.keys())

    # Stats
    total_edges = sum(len(v) for v in graph.values())
    isolated = sum(1 for sid in symbol_id_set
                   if len(graph.get(sid, [])) == 0
                   and len(reverse_graph.get(sid, set())) == 0)

    print(f"  Symbols: {len(symbols)}  Edges: {total_edges}  Isolated: {isolated} ({100*isolated/len(symbols):.1f}%)")
    print(f"  Cases: {len(cases)}")

    # ── Analyze each case ────────────────────────────────────────────────
    relationship_counter = Counter()
    missing_edge_types = Counter()
    query_degree_when_miss = []
    gt_degree_when_miss = []
    hit_cases = 0
    miss_cases = 0
    total_gt_symbols = 0
    gt_in_graph_somewhere = 0
    query_in_graph_somewhere = 0

    detailed_misses = []

    for case in cases:
        q = case.query_symbol
        gt_syms = set(case.ground_truth_symbols) & symbol_id_set
        if not gt_syms or q not in symbol_id_set:
            continue

        total_gt_symbols += len(gt_syms)

        # Check if query has ANY edges
        q_out = len(graph.get(q, []))
        q_in = len(reverse_graph.get(q, set()))
        q_total = q_out + q_in
        if q_total > 0:
            query_in_graph_somewhere += 1

        # Check graph reachability for each GT symbol
        q_reachable = set()
        # BFS from query (forward + backward, depth 3)
        visited = set()
        frontier = {q}
        for depth in range(3):
            next_frontier = set()
            for node in frontier:
                if node in visited:
                    continue
                visited.add(node)
                for neighbor in graph.get(node, []):
                    next_frontier.add(neighbor)
                for neighbor in reverse_graph.get(node, set()):
                    next_frontier.add(neighbor)
            frontier = next_frontier - visited
        q_reachable = visited - {q}

        for gt_sym in gt_syms:
            gt_out = len(graph.get(gt_sym, []))
            gt_in = len(reverse_graph.get(gt_sym, set()))
            gt_total = gt_out + gt_in
            if gt_total > 0:
                gt_in_graph_somewhere += 1

            # Classify the RELATIONSHIP between query and GT
            q_file = q.split(":")[0]
            gt_file = gt_sym.split(":")[0]
            q_name = q.split(":", 1)[1] if ":" in q else q
            gt_name = gt_sym.split(":", 1)[1] if ":" in gt_sym else gt_sym
            q_class = q_name.rsplit(".", 1)[0] if "." in q_name else None
            gt_class = gt_name.rsplit(".", 1)[0] if "." in gt_name else None

            if q_file == gt_file and q_class and gt_class and q_class == gt_class:
                rel = "same_file_same_class"
            elif q_file == gt_file:
                rel = "same_file_diff_class_or_free"
            elif os.path.dirname(q_file) == os.path.dirname(gt_file):
                rel = "same_package"
            else:
                rel = "cross_package"
            relationship_counter[rel] += 1

            # Is this GT reachable via graph?
            if gt_sym in q_reachable:
                hit_cases += 1
            else:
                miss_cases += 1
                query_degree_when_miss.append(q_total)
                gt_degree_when_miss.append(gt_total)

                # Classify WHY we missed
                if q_total == 0 and gt_total == 0:
                    missing_edge_types["both_isolated"] += 1
                elif q_total == 0:
                    missing_edge_types["query_isolated"] += 1
                elif gt_total == 0:
                    missing_edge_types["gt_isolated"] += 1
                else:
                    missing_edge_types["both_connected_but_unreachable"] += 1

                if len(detailed_misses) < 15:
                    detailed_misses.append({
                        "query": q,
                        "gt": gt_sym,
                        "relationship": rel,
                        "q_degree": q_total,
                        "gt_degree": gt_total,
                        "q_edges_out": graph.get(q, [])[:5],
                        "gt_edges_out": graph.get(gt_sym, [])[:5],
                    })

    # ── Print results ────────────────────────────────────────────────────
    print(f"\n  ── Ground Truth Relationship Types ──")
    for rel, count in relationship_counter.most_common():
        print(f"    {rel:<35} {count:>5}  ({100*count/total_gt_symbols:.1f}%)")

    print(f"\n  ── Graph Reachability (BFS depth 3) ──")
    print(f"    Hit (GT reachable from query):    {hit_cases:>5}")
    print(f"    Miss (GT NOT reachable):           {miss_cases:>5}")
    if hit_cases + miss_cases > 0:
        print(f"    Graph reachability rate:           {100*hit_cases/(hit_cases+miss_cases):.1f}%")

    print(f"\n  ── Why Graph Misses ──")
    for reason, count in missing_edge_types.most_common():
        print(f"    {reason:<40} {count:>5}  ({100*count/max(miss_cases,1):.1f}%)")

    if query_degree_when_miss:
        avg_q = sum(query_degree_when_miss) / len(query_degree_when_miss)
        avg_gt = sum(gt_degree_when_miss) / len(gt_degree_when_miss)
        zero_q = sum(1 for d in query_degree_when_miss if d == 0)
        zero_gt = sum(1 for d in gt_degree_when_miss if d == 0)
        print(f"\n  ── Degree stats on misses ──")
        print(f"    Avg query degree:  {avg_q:.1f}  (zero: {zero_q}/{len(query_degree_when_miss)})")
        print(f"    Avg GT degree:     {avg_gt:.1f}  (zero: {zero_gt}/{len(gt_degree_when_miss)})")

    print(f"\n  ── Detailed Miss Examples (first 15) ──")
    for m in detailed_misses:
        print(f"\n    Query:  {m['query']}")
        print(f"    GT:     {m['gt']}")
        print(f"    Rel:    {m['relationship']}  Q-deg={m['q_degree']}  GT-deg={m['gt_degree']}")
        if m['q_edges_out']:
            print(f"    Q calls: {m['q_edges_out'][:3]}")
        if m['gt_edges_out']:
            print(f"    GT calls: {m['gt_edges_out'][:3]}")

    # ── Analyze what KINDS of edges we're missing ────────────────────────
    print(f"\n  ── Missing Edge Type Analysis ──")
    print(f"  What new edge types would help the most?")

    # Check: how many GT pairs share imports?
    # Check: how many GT pairs share class hierarchy?
    # Check: how many GT pairs reference common symbols?
    print(f"\n  Query symbols with edges in graph: {query_in_graph_somewhere}/{len(cases)}")
    print(f"  GT symbols with edges in graph:    {gt_in_graph_somewhere}/{total_gt_symbols}")

    # Analyze edge types that exist
    edge_type_counter = Counter()
    for src, targets in graph.items():
        src_file = src.split(":")[0]
        for tgt in targets:
            tgt_file = tgt.split(":")[0]
            if src_file == tgt_file:
                edge_type_counter["intra_file_call"] += 1
            else:
                edge_type_counter["cross_file_call"] += 1
    print(f"\n  ── Existing Edge Distribution ──")
    for etype, count in edge_type_counter.most_common():
        print(f"    {etype:<30} {count:>6}  ({100*count/total_edges:.1f}%)")


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "benchmark_repos/transformers"
    diagnose(repo)
