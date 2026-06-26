#!/usr/bin/env python3
"""
eval_v1.py — DiffContext Evaluation Framework v1.0

Implements research-quality evaluation:
  1. Extended IR metrics: P@K, R@K (K=5,10,20,50), MRR, MAP, nDCG@20
  2. Per-signal ablation: Graph | BM25 | File | Random | Graph+BM25 | Graph+File | All
  3. Failure taxonomy: classifies every zero-recall case by root cause
  4. Graph health stats: isolation, connectivity, degree distribution
  5. Bootstrap 95% confidence intervals (1000 resamples)

Usage:
  python benchmarks/eval_v1.py                         # all repos
  python benchmarks/eval_v1.py benchmark_repos/flask   # single repo
"""

import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import get_blast_radius, build_reverse_graph
from diffcontext.impact.traversal import expand_dependencies
from diffcontext.impact.scoring import compute_impact_scores
from benchmarks.ground_truth import extract_cochange_cases
from benchmarks.baselines import BM25Baseline, FileCoLocationBaseline, RandomBaseline, _tokenize

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
K_VALUES      = [5, 10, 20, 50, 100]
N_BOOTSTRAP   = 1000
CI_LEVEL      = 0.95
MAX_CASES     = 50
CANDIDATE_LIMIT = 100
TOKEN_BUDGET    = 10_000
RRF_K           = 60
SIGNALS = [
    "graph", "bm25", "file", "random",
    "graph+bm25", "graph+file", "graph+bm25+file",
    "graph+bm25+file+rrf",
    "oracle_top100_union",
]


# ---------------------------------------------------------------------------
# IR Metrics
# ---------------------------------------------------------------------------

def compute_metrics(ranked: List[str], gt: Set[str]) -> Dict:
    """
    Compute all IR metrics given a ranked retrieval list and binary ground truth.

    ranked : items in score order, most relevant first (query excluded)
    gt     : set of truly relevant items
    """
    if not gt:
        return _zero_metrics()

    gt_size = len(gt)
    ranked_set = set(ranked)

    # ── Unlimited P / R / F1 ─────────────────────────────────────────────
    tp_all = len(ranked_set & gt)
    prec   = tp_all / len(ranked_set) if ranked_set else 0.0
    rec    = tp_all / gt_size
    f1     = 2*prec*rec/(prec+rec) if (prec+rec) > 0 else 0.0

    # ── P@K / R@K ────────────────────────────────────────────────────────
    pk, rk = {}, {}
    for k in K_VALUES:
        top_k_set = set(ranked[:k])
        tp_k      = len(top_k_set & gt)
        pk[k] = tp_k / k
        rk[k] = tp_k / gt_size

    # ── MRR ──────────────────────────────────────────────────────────────
    mrr = 0.0
    for rank, sym in enumerate(ranked, 1):
        if sym in gt:
            mrr = 1.0 / rank
            break

    # ── MAP ──────────────────────────────────────────────────────────────
    ap, hits = 0.0, 0
    for rank, sym in enumerate(ranked, 1):
        if sym in gt:
            hits += 1
            ap   += hits / rank
    map_score = ap / gt_size   # divide by |GT|, not |hits|

    # ── nDCG@20 ──────────────────────────────────────────────────────────
    K_NDCG = 20
    dcg  = sum(1.0 / math.log2(rank + 1)
               for rank, sym in enumerate(ranked[:K_NDCG], 1) if sym in gt)
    idcg = sum(1.0 / math.log2(rank + 1)
               for rank in range(1, min(gt_size, K_NDCG) + 1))
    ndcg = dcg / idcg if idcg > 0 else 0.0

    result: Dict = {
        "precision": prec, "recall": rec, "f1": f1,
        "mrr": mrr, "map": map_score, "ndcg@20": ndcg,
    }
    for k in K_VALUES:
        result[f"p@{k}"] = pk[k]
        result[f"r@{k}"] = rk[k]
    return result


def _zero_metrics() -> Dict:
    base = {"precision": 0.0, "recall": 0.0, "f1": 0.0,
            "mrr": 0.0, "map": 0.0, "ndcg@20": 0.0}
    for k in K_VALUES:
        base[f"p@{k}"] = 0.0
        base[f"r@{k}"] = 0.0
    return base


def avg_metrics(metric_list: List[Dict]) -> Dict:
    """Element-wise mean over a list of metric dicts."""
    if not metric_list:
        return _zero_metrics()
    keys = metric_list[0].keys()
    return {k: sum(m[k] for m in metric_list) / len(metric_list) for k in keys}


def round_metrics(m: Dict, ndigits: int = 4) -> Dict:
    return {k: round(v, ndigits) for k, v in m.items()}


def _token_estimate(symbols, ranked: List[str]) -> int:
    return sum(max(1, len(symbols[s].code) // 4) for s in ranked if s in symbols)


def retrieval_stats(symbols, ranked_lists: List[List[str]]) -> Dict:
    """Prompt-size stats for each signal under the shared candidate cap."""
    if not ranked_lists:
        return {
            "avg_retrieved_symbols": 0.0,
            "avg_prompt_tokens": 0.0,
            "avg_symbol_size_tokens": 0.0,
            "budget_utilization_pct": 0.0,
        }

    counts = [len(ranked) for ranked in ranked_lists]
    token_counts = [_token_estimate(symbols, ranked) for ranked in ranked_lists]
    avg_count = sum(counts) / len(counts)
    avg_tokens = sum(token_counts) / len(token_counts)
    avg_symbol_size = avg_tokens / avg_count if avg_count else 0.0
    return {
        "avg_retrieved_symbols": round(avg_count, 2),
        "avg_prompt_tokens": round(avg_tokens, 1),
        "avg_symbol_size_tokens": round(avg_symbol_size, 1),
        "budget_utilization_pct": round(100 * avg_tokens / TOKEN_BUDGET, 1),
    }


def _rrf_ranked(ranked_lists: List[List[str]], top_n: int = CANDIDATE_LIMIT) -> List[str]:
    """Reciprocal Rank Fusion over existing ranked candidate lists."""
    scores: Dict[str, float] = defaultdict(float)
    best_rank: Dict[str, int] = {}

    for ranked in ranked_lists:
        for rank, sid in enumerate(ranked[:top_n], 1):
            scores[sid] += 1.0 / (RRF_K + rank)
            best_rank[sid] = min(rank, best_rank.get(sid, rank))

    return [
        sid for sid, _ in sorted(
            scores.items(),
            key=lambda item: (-item[1], best_rank[item[0]], item[0]),
        )
    ][:top_n]


def contribution_analysis(
    cases,
    symbol_id_set: Set[str],
    ranked_by_signal: Dict[str, List[List[str]]],
) -> Dict:
    """
    Count ground-truth symbol instances recovered by Graph/BM25/File patterns.

    A GT symbol is counted once per eval case, under the exact subset of
    retrievers whose top-100 candidate set contains it.
    """
    labels = {
        "graph": "graph_only",
        "bm25": "bm25_only",
        "file": "file_only",
        "graph+bm25": "graph_bm25",
        "graph+file": "graph_file",
        "bm25+file": "bm25_file",
        "graph+bm25+file": "graph_bm25_file",
        "missed": "missed",
    }
    counts = {label: 0 for label in labels.values()}
    total_gt = 0

    graph_lists = ranked_by_signal.get("graph", [])
    bm25_lists = ranked_by_signal.get("bm25", [])
    file_lists = ranked_by_signal.get("file", [])

    for idx, case in enumerate(cases):
        if idx >= len(graph_lists) or idx >= len(bm25_lists) or idx >= len(file_lists):
            break

        gt = set(case.ground_truth_symbols) & symbol_id_set
        total_gt += len(gt)
        candidate_sets = {
            "graph": set(graph_lists[idx][:CANDIDATE_LIMIT]),
            "bm25": set(bm25_lists[idx][:CANDIDATE_LIMIT]),
            "file": set(file_lists[idx][:CANDIDATE_LIMIT]),
        }

        for gt_symbol in gt:
            present = [
                name for name in ("graph", "bm25", "file")
                if gt_symbol in candidate_sets[name]
            ]
            key = "+".join(present) if present else "missed"
            counts[labels[key]] += 1

    rows = {
        key: {
            "count": count,
            "pct": round(100 * count / total_gt, 1) if total_gt else 0.0,
        }
        for key, count in counts.items()
    }
    return {"total_gt_instances": total_gt, "patterns": rows}


# ---------------------------------------------------------------------------
# Bootstrap Confidence Intervals
# ---------------------------------------------------------------------------

def bootstrap_ci(
    values: List[float],
    n_bootstrap: int = N_BOOTSTRAP,
    ci: float = CI_LEVEL,
    seed: int = 42,
) -> Tuple[float, float]:
    """Return (lower, upper) bootstrap CI for the mean of `values`."""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n   = len(values)
    means = sorted(
        sum(values[rng.randint(0, n-1)] for _ in range(n)) / n
        for _ in range(n_bootstrap)
    )
    lo = means[int((1 - ci) / 2 * n_bootstrap)]
    hi = means[min(int((1 + ci) / 2 * n_bootstrap), n_bootstrap - 1)]
    return (round(lo, 4), round(hi, 4))


def ci_for_metrics(metric_list: List[Dict]) -> Dict:
    """Return {metric_name: (lo, hi)} bootstrap CIs."""
    if not metric_list:
        return {}
    keys = metric_list[0].keys()
    return {
        k: bootstrap_ci([m[k] for m in metric_list])
        for k in keys
    }


# ---------------------------------------------------------------------------
# Graph Health Statistics
# ---------------------------------------------------------------------------

def graph_health(
    graph: Dict[str, List[str]],
    all_symbol_ids: Set[str],
) -> Dict:
    """
    Compute graph topology stats.

    Isolated node = in-degree 0 AND out-degree 0 (completely disconnected).
    Connected components counted on UNDIRECTED version of the graph.
    """
    nodes = set(graph.keys()) | all_symbol_ids
    n     = len(nodes)

    out_deg: Dict[str, int] = {nd: len(graph.get(nd, [])) for nd in nodes}
    in_deg:  Dict[str, int] = defaultdict(int)
    for nd, edges in graph.items():
        for e in edges:
            in_deg[e] += 1

    total_edges = sum(out_deg.values())
    isolated    = sum(1 for nd in nodes if out_deg[nd] == 0 and in_deg[nd] == 0)

    # Union-Find for weakly-connected components
    parent = {nd: nd for nd in nodes}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    for nd, edges in graph.items():
        for e in edges:
            if e in parent:
                _union(nd, e)

    comp_sizes: Dict[str, int] = defaultdict(int)
    for nd in nodes:
        comp_sizes[_find(nd)] += 1
    sizes = sorted(comp_sizes.values(), reverse=True)

    return {
        "total_symbols":          n,
        "total_edges":            total_edges,
        "edge_density":           round(total_edges / n, 3) if n else 0,
        "isolated_nodes":         isolated,
        "isolated_pct":           round(100 * isolated / n, 1) if n else 0,
        "avg_out_degree":         round(sum(out_deg.values()) / n, 3) if n else 0,
        "avg_in_degree":          round(sum(in_deg.values()) / n, 3) if n else 0,
        "max_out_degree":         max(out_deg.values(), default=0),
        "connected_components":   len(sizes),
        "largest_component":      sizes[0] if sizes else 0,
        "largest_component_pct":  round(100 * sizes[0] / n, 1) if sizes else 0,
        "singleton_components":   sum(1 for s in sizes if s == 1),
    }


# ---------------------------------------------------------------------------
# Failure Taxonomy
# ---------------------------------------------------------------------------

FAILURE_QUERY_ISOLATED   = "query_isolated"        # query: in=0, out=0
FAILURE_GT_ISOLATED      = "gt_isolated"           # GT symbols all isolated
FAILURE_DISCONNECTED     = "disconnected_graph"    # neither isolated but no path
FAILURE_RANKING          = "ranking_error"         # GT reachable but ranked too low
FAILURE_NO_RETRIEVAL     = "no_retrieval"          # ranked list empty (non-isolated)
FAILURE_BUDGET           = "token_budget"          # (placeholder — needs budget info)


def classify_failure(
    query: str,
    gt: Set[str],
    graph: Dict[str, List[str]],
    reverse_graph: Dict[str, Set[str]],
    ranked: List[str],
) -> str:
    """Classify root cause for a zero-recall case."""
    out_deg = len(graph.get(query, []))
    in_deg  = len(reverse_graph.get(query, set()))
    query_isolated = (out_deg == 0 and in_deg == 0)

    if not ranked:
        return FAILURE_QUERY_ISOLATED if query_isolated else FAILURE_NO_RETRIEVAL

    # Check if GT symbols were at least somewhere in ranked (any position)
    ranked_set = set(ranked)
    if ranked_set & gt:
        return FAILURE_RANKING   # GT was present but not in top-K

    # GT not in ranked at all — why?
    gt_all_isolated = all(
        len(graph.get(g, [])) == 0 and len(reverse_graph.get(g, set())) == 0
        for g in gt
    )
    if gt_all_isolated:
        return FAILURE_GT_ISOLATED
    if query_isolated:
        return FAILURE_QUERY_ISOLATED
    return FAILURE_DISCONNECTED


# ---------------------------------------------------------------------------
# Signal Runners
# ---------------------------------------------------------------------------

def _graph_ranked(
    query: str,
    graph: Dict[str, List[str]],
    reverse_graph: Dict[str, Set[str]],
    top_n: int = CANDIDATE_LIMIT,
) -> List[str]:
    """Graph signal: bidirectional BFS decay scoring."""
    blast_radii = {query: get_blast_radius(graph, query, reverse=reverse_graph)}
    seed        = [query] + blast_radii[query]
    expanded    = expand_dependencies(graph, seed, max_depth=2)
    scores      = compute_impact_scores(
        graph, [query], blast_radii,
        expanded_deps=expanded, reverse=reverse_graph,
    )
    ranked = [
        s for s, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)
        if s != query
    ]
    return ranked[:top_n]


def _normalize(scores: Dict[str, float]) -> Dict[str, float]:
    """Min-max normalize a score dict to [0, 1]."""
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    if hi == lo:
        return {k: 0.5 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


def _graph_scores(
    query: str,
    graph: Dict[str, List[str]],
    reverse_graph: Dict[str, Set[str]],
) -> Dict[str, float]:
    blast_radii = {query: get_blast_radius(graph, query, reverse=reverse_graph)}
    seed        = [query] + blast_radii[query]
    expanded    = expand_dependencies(graph, seed, max_depth=2)
    raw = compute_impact_scores(
        graph, [query], blast_radii,
        expanded_deps=expanded, reverse=reverse_graph,
    )
    raw.pop(query, None)
    return raw


def run_ablation(
    cases,
    symbols,
    graph,
    reverse_graph,
    symbol_id_set: Set[str],
    bm25: BM25Baseline,
    file_bl: FileCoLocationBaseline,
    rand_bl: RandomBaseline,
) -> Tuple[Dict[str, List[Dict]], Dict[str, List[List[str]]]]:
    """
    Run signals on every case.

    Returns:
      - {signal_name: [metric_dict_per_case]}
      - {signal_name: [ranked_symbol_ids_per_case]}

    Signals:
      graph        — call graph only
      bm25         — BM25 keyword only
      file         — same-file only
      random       — random baseline
      graph+bm25   — normalized blend (0.6 graph + 0.4 bm25)
      graph+file   — normalized blend (0.7 graph + 0.3 file)
      graph+bm25+file — full blend
      graph+bm25+file+rrf — reciprocal rank fusion over Graph/BM25/File
    """
    results: Dict[str, List[Dict]] = {signal: [] for signal in SIGNALS}
    ranked_by_signal: Dict[str, List[List[str]]] = {signal: [] for signal in SIGNALS}

    symbol_ids = list(symbols.keys())

    for case in cases:
        q  = case.query_symbol
        gt = set(case.ground_truth_symbols) & symbol_id_set
        if not gt or q not in symbol_id_set:
            continue

        # ── Individual signals ─────────────────────────────────────────────
        g_ranked = _graph_ranked(q, graph, reverse_graph)
        b_ranked = bm25.retrieve(q, top_k=CANDIDATE_LIMIT)
        f_ranked = file_bl.retrieve(q, top_k=CANDIDATE_LIMIT)
        r_ranked = rand_bl.retrieve(q, top_k=CANDIDATE_LIMIT)

        results["graph"].append(compute_metrics(g_ranked, gt))
        results["bm25"].append(compute_metrics(b_ranked, gt))
        results["file"].append(compute_metrics(f_ranked, gt))
        results["random"].append(compute_metrics(r_ranked, gt))
        ranked_by_signal["graph"].append(g_ranked)
        ranked_by_signal["bm25"].append(b_ranked)
        ranked_by_signal["file"].append(f_ranked)
        ranked_by_signal["random"].append(r_ranked)

        # ── Hybrid signals (normalized score blending) ─────────────────────
        g_scores = _normalize(_graph_scores(q, graph, reverse_graph))

        # BM25 raw scores as dict
        q_tokens  = _tokenize(symbols[q].code)
        bm25_raw  = bm25.bm25.get_scores(q_tokens)
        b_scores  = _normalize({sid: bm25_raw[i] for i, sid in enumerate(symbol_ids)
                                  if sid != q and bm25_raw[i] > 0})

        # File scores: 1 if same file else 0
        q_file    = q.split(":")[0] if ":" in q else ""
        f_scores  = {sid: 1.0 for sid in symbol_ids
                      if sid != q and sid.split(":")[0] == q_file}

        def _blend_ranked(weighted: List[Tuple[Dict[str, float], float]]) -> List[str]:
            combined: Dict[str, float] = defaultdict(float)
            for score_dict, weight in weighted:
                for sid, sc in score_dict.items():
                    combined[sid] += weight * sc
            return [s for s, _ in sorted(combined.items(), key=lambda x: x[1], reverse=True)
                    if s != q][:CANDIDATE_LIMIT]

        gb_ranked  = _blend_ranked([(g_scores, 0.6), (b_scores, 0.4)])
        gf_ranked  = _blend_ranked([(g_scores, 0.7), (f_scores, 0.3)])
        gbf_ranked = _blend_ranked([(g_scores, 0.5), (b_scores, 0.35), (f_scores, 0.15)])
        rrf_ranked = _rrf_ranked([g_ranked, b_ranked, f_ranked])

        results["graph+bm25"].append(compute_metrics(gb_ranked, gt))
        results["graph+file"].append(compute_metrics(gf_ranked, gt))
        results["graph+bm25+file"].append(compute_metrics(gbf_ranked, gt))
        results["graph+bm25+file+rrf"].append(compute_metrics(rrf_ranked, gt))
        ranked_by_signal["graph+bm25"].append(gb_ranked)
        ranked_by_signal["graph+file"].append(gf_ranked)
        ranked_by_signal["graph+bm25+file"].append(gbf_ranked)
        ranked_by_signal["graph+bm25+file+rrf"].append(rrf_ranked)

        oracle_seen = set()
        oracle_ranked = []
        for ranked in (g_ranked, b_ranked, f_ranked):
            for sid in ranked[:CANDIDATE_LIMIT]:
                if sid not in oracle_seen:
                    oracle_seen.add(sid)
                    oracle_ranked.append(sid)

        results["oracle_top100_union"].append(compute_metrics(oracle_ranked, gt))
        ranked_by_signal["oracle_top100_union"].append(oracle_ranked)

    return results, ranked_by_signal


# ---------------------------------------------------------------------------
# Per-Repo Evaluation
# ---------------------------------------------------------------------------

def evaluate_repo(repo_path: str, max_cases: int = MAX_CASES) -> Optional[Dict]:
    repo_name = os.path.basename(os.path.abspath(repo_path))
    print(f"\n{'='*64}")
    print(f"  {repo_name}")
    print(f"{'='*64}")

    cases = extract_cochange_cases(repo_path, max_cases=max_cases)
    if not cases:
        print("  SKIP — no co-change cases found.")
        return None
    print(f"  Cases extracted: {len(cases)}")

    # ── Build infrastructure ───────────────────────────────────────────────
    t0 = time.perf_counter()
    symbols = extract_all_symbols(repo_path)
    graph   = build_repository_graph(repo_path)
    build_s = time.perf_counter() - t0
    reverse_graph = build_reverse_graph(graph)
    symbol_id_set = set(symbols.keys())

    print(f"  Symbols: {len(symbols)}  Edges: {sum(len(v) for v in graph.values())}  "
          f"Build: {build_s:.1f}s")

    # ── Graph health ───────────────────────────────────────────────────────
    health = graph_health(graph, symbol_id_set)
    print(f"  Graph health: isolated={health['isolated_pct']}%  "
          f"components={health['connected_components']}  "
          f"largest={health['largest_component_pct']}%")

    # ── Baselines ──────────────────────────────────────────────────────────
    bm25    = BM25Baseline(symbols)
    file_bl = FileCoLocationBaseline(symbols)
    rand_bl = RandomBaseline(symbols)

    # ── Filter valid cases ─────────────────────────────────────────────────
    valid_cases = [
        c for c in cases
        if c.query_symbol in symbol_id_set
        and (set(c.ground_truth_symbols) & symbol_id_set)
    ]
    skipped = len(cases) - len(valid_cases)
    print(f"  Valid cases: {len(valid_cases)} / {len(cases)}  (skipped {skipped}: query or GT not in symbols)")

    if not valid_cases:
        print("  SKIP — no valid cases after filter.")
        return None

    # ── Ablation ──────────────────────────────────────────────────────────
    print(f"  Running ablation ({len(valid_cases)} cases x {len(SIGNALS)} signals, top-{CANDIDATE_LIMIT} cap) ...")
    abl_raw, ranked_by_signal = run_ablation(
        valid_cases, symbols, graph, reverse_graph,
        symbol_id_set, bm25, file_bl, rand_bl,
    )

    ablation: Dict[str, Dict] = {}
    for signal, metrics_list in abl_raw.items():
        if not metrics_list:
            continue
        avg = avg_metrics(metrics_list)
        cis = ci_for_metrics(metrics_list)
        ablation[signal] = {
            "n": len(metrics_list),
            "avg": round_metrics(avg),
            "ci_95": {k: list(v) for k, v in cis.items()},
            "zero_recall_cases": sum(1 for m in metrics_list if m["recall"] == 0),
            "retrieval_stats": retrieval_stats(symbols, ranked_by_signal.get(signal, [])),
        }

    contribution = contribution_analysis(valid_cases, symbol_id_set, ranked_by_signal)

    # ── Failure taxonomy (graph signal only) ─────────────────────────────
    failure_counts: Dict[str, int] = defaultdict(int)
    failure_cases:  Dict[str, List[str]] = defaultdict(list)

    for case in valid_cases:
        q  = case.query_symbol
        gt = set(case.ground_truth_symbols) & symbol_id_set
        g_ranked = _graph_ranked(q, graph, reverse_graph)
        if not (set(g_ranked) & gt):   # zero recall
            reason = classify_failure(q, gt, graph, reverse_graph, g_ranked)
            failure_counts[reason] += 1
            failure_cases[reason].append(q)

    total_zero = sum(failure_counts.values())

    # ── Print ablation table ──────────────────────────────────────────────
    print(f"\n  ── Ablation Table ({repo_name}) ──")
    cols = ["recall", "precision", "f1", "mrr", "map", "ndcg@20",
            "r@5", "r@10", "r@20", "r@50", "r@100"]
    header = f"  {'Signal':<22}" + "".join(f"{c:>10}" for c in cols)
    print(header)
    print("  " + "-" * (22 + 10*len(cols)))
    for sig, data in ablation.items():
        avg = data["avg"]
        row = f"  {sig:<22}" + "".join(f"{avg.get(c,0):>10.4f}" for c in cols)
        print(row)

    print(f"\n  ── Retrieval Stats (top-{CANDIDATE_LIMIT}, {TOKEN_BUDGET:,}-token budget) ──")
    stats_header = f"  {'Signal':<22}{'avg_ret':>9}{'avg_tok':>10}{'tok/sym':>10}{'budget%':>9}"
    print(stats_header)
    print("  " + "-" * 60)
    for sig, data in ablation.items():
        stats = data["retrieval_stats"]
        print(
            f"  {sig:<22}"
            f"{stats['avg_retrieved_symbols']:>9.2f}"
            f"{stats['avg_prompt_tokens']:>10.1f}"
            f"{stats['avg_symbol_size_tokens']:>10.1f}"
            f"{stats['budget_utilization_pct']:>9.1f}"
        )

    oracle = ablation.get("oracle_top100_union", {}).get("avg", {})
    if oracle:
        print(
            f"\n  Oracle Top100 union recall: {oracle.get('recall', 0):.4f} "
            f"(candidate-generation ceiling from Graph U BM25 U File)"
        )

    print(f"\n  ── Contribution Analysis (GT instances in top-{CANDIDATE_LIMIT}) ──")
    print(f"  {'Pattern':<22}{'count':>8}{'pct':>8}")
    print("  " + "-" * 38)
    for pattern, data in contribution["patterns"].items():
        print(f"  {pattern:<22}{data['count']:>8}{data['pct']:>7.1f}%")
    print(f"  {'total_gt_instances':<22}{contribution['total_gt_instances']:>8}")

    # ── Print failure taxonomy ────────────────────────────────────────────
    print(f"\n  ── Failure Taxonomy (graph signal, {total_zero} zero-recall cases) ──")
    for reason, count in sorted(failure_counts.items(), key=lambda x: -x[1]):
        pct = 100 * count / max(len(valid_cases), 1)
        print(f"    {reason:<25} {count:>4}  ({pct:.0f}% of cases)")

    # ── Print graph health ────────────────────────────────────────────────
    print(f"\n  ── Graph Health ──")
    for k, v in health.items():
        print(f"    {k:<30} {v}")

    return {
        "repo":         repo_name,
        "n_cases":      len(valid_cases),
        "config": {
            "candidate_limit": CANDIDATE_LIMIT,
            "token_budget": TOKEN_BUDGET,
            "rrf_k": RRF_K,
            "k_values": K_VALUES,
            "random_seed": 42,
        },
        "graph_health": health,
        "ablation":     ablation,
        "contribution_analysis": contribution,
        "failure_taxonomy": {
            "total_zero_recall": total_zero,
            "counts": dict(failure_counts),
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
        repo_paths = [sys.argv[1]]
    else:
        bench_dir = os.path.join(os.path.dirname(__file__), "..", "benchmark_repos")
        bench_dir = os.path.normpath(bench_dir)
        if not os.path.isdir(bench_dir):
            print(f"benchmark_repos not found at {bench_dir}")
            sys.exit(1)
        repo_paths = sorted(
            os.path.join(bench_dir, name)
            for name in os.listdir(bench_dir)
            if os.path.isdir(os.path.join(bench_dir, name, ".git"))
        )

    all_results = []
    for rp in repo_paths:
        result = evaluate_repo(rp)
        if result:
            all_results.append(result)

    # ── Aggregate summary ─────────────────────────────────────────────────
    print(f"\n{'='*64}")
    print("  AGGREGATE: Recall@10 across all repos (primary metric)")
    print(f"{'='*64}")
    signals = SIGNALS
    hdr = f"  {'Signal':<22}" + "".join(f"{r['repo']:>12}" for r in all_results) + f"{'mean':>12}"
    print(hdr)
    print("  " + "-" * (22 + 12*(len(all_results)+1)))
    for sig in signals:
        vals = []
        row  = f"  {sig:<22}"
        for r in all_results:
            v = r["ablation"].get(sig, {}).get("avg", {}).get("r@10", 0.0)
            row += f"{v:>12.4f}"
            vals.append(v)
        mean_v = sum(vals) / len(vals) if vals else 0.0
        row += f"{mean_v:>12.4f}"
        print(row)

    print(f"\n{'='*64}")
    print("  AGGREGATE: Ranking metrics across all repos")
    print(f"{'='*64}")
    metric_cols = ["r@10", "map", "mrr", "ndcg@20", "precision"]
    print(f"  {'Signal':<24}" + "".join(f"{c:>11}" for c in metric_cols))
    print("  " + "-" * (24 + 11*len(metric_cols)))
    for sig in signals:
        row = f"  {sig:<24}"
        for metric in metric_cols:
            vals = [
                r["ablation"].get(sig, {}).get("avg", {}).get(metric, 0.0)
                for r in all_results
            ]
            mean_v = sum(vals) / len(vals) if vals else 0.0
            row += f"{mean_v:>11.4f}"
        print(row)

    print(f"\n{'='*64}")
    print("  AGGREGATE: Oracle Top100 union recall")
    print(f"{'='*64}")
    vals = []
    for r in all_results:
        v = r["ablation"].get("oracle_top100_union", {}).get("avg", {}).get("recall", 0.0)
        vals.append(v)
        print(f"  {r['repo']:<22}{v:>12.4f}")
    mean_v = sum(vals) / len(vals) if vals else 0.0
    print(f"  {'mean':<22}{mean_v:>12.4f}")

    print(f"\n{'='*64}")
    print("  AGGREGATE: Contribution Analysis")
    print(f"{'='*64}")
    aggregate_patterns: Dict[str, int] = defaultdict(int)
    aggregate_total = 0
    for r in all_results:
        contribution = r.get("contribution_analysis", {})
        aggregate_total += contribution.get("total_gt_instances", 0)
        for pattern, data in contribution.get("patterns", {}).items():
            aggregate_patterns[pattern] += data.get("count", 0)

    print(f"  {'Pattern':<22}{'count':>8}{'pct':>8}")
    print("  " + "-" * 38)
    for pattern, count in aggregate_patterns.items():
        pct = 100 * count / aggregate_total if aggregate_total else 0.0
        print(f"  {pattern:<22}{count:>8}{pct:>7.1f}%")
    print(f"  {'total_gt_instances':<22}{aggregate_total:>8}")

    # ── Save JSON without overwriting eval_v1.json ────────────────────────
    out_dir  = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    for result in all_results:
        repo_file = os.path.join(out_dir, f"eval_{result['repo']}.json")
        with open(repo_file, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\n  Repo result saved to {repo_file}")

    out_file = os.path.join(out_dir, "eval_all.json")
    with open(out_file, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n  Aggregate results saved to {out_file}")
    print(f"  Existing eval_v1.json was not overwritten.")
    print(f"\n  FREEZE THIS BENCHMARK CONFIG. Do not compare future runs with different caps.")
    print(f"  Any retrieval improvement must beat these numbers on R@10, MAP, MRR.")


if __name__ == "__main__":
    main()
