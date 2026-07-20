#!/usr/bin/env python3
"""
blend_loro.py — Leave-one-repo-out validation of the hybrid blend weights,
plus an adaptive (per-query dynamic) blend.

Why this exists (the methodological hole it closes): the shipped hybrid
weights (0.5 graph / 0.35 BM25 / 0.15 same-file) were selected by eval_v1
on the SAME five repos that eval_v2 reports as the headline result. That is
tuning and testing on the same data. This harness answers three questions
a reviewer will ask:

  Q1  Do the shipped weights survive leave-one-repo-out (LORO) selection —
      when the reporting repo is excluded from weight selection, do the
      selected weights (and the held-out score) materially change?
  Q2  Does a per-query ADAPTIVE blend — graph weight scaled by the graph's
      own evidence for that query — beat any fixed blend, in particular in
      the graph-blind regime (pydantic) without giving back the graph-dense
      wins (django)?
  Q3  Do the conclusions hold on repos never used for any selection at all
      (--holdout)?

Protocol:
  * Same commit mining, token budget, candidate cap, and metrics as
    eval_v2_hardened (imported from it / eval_v1, not re-implemented).
  * Component scores (graph / BM25 / same-file / optionally dense) are
    computed ONCE per query; the weight grid is then evaluated by matrix
    multiply, so the full simplex sweep costs about one eval pass.
  * Primary selection objective: per-commit mean recall (a commit counts
    once). R@20, hit, precision, F1 are recorded but not used for selection.
  * Paired significance: sign-flip permutation test on per-commit recall
    differences (both methods see identical commits).

Usage:
  python benchmarks/blend_loro.py                     # 5 dev repos, 3-leg grid
  python benchmarks/blend_loro.py --dense             # + dense leg (4-leg grid)
  python benchmarks/blend_loro.py --holdout black requests rich starlette
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import build_reverse_graph

from benchmarks.baselines import BM25Baseline, EmbeddingBaseline, _tokenize
from benchmarks.eval_v1 import (
    _graph_scores, _normalize,
    TOKEN_BUDGET, CANDIDATE_LIMIT,
)
from benchmarks.eval_v2_hardened import extract_distinct_commits

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "loro")
DEV_REPOS = ["click", "django", "flask", "httpx", "pydantic"]
SHIPPED = (0.5, 0.35, 0.15)          # graph, bm25, samefile — pipeline.py HYBRID_WEIGHTS
GRID_STEP = 0.05
PER_COMPONENT_CAP = 400              # per-component candidate cap (rank fusion depth)
ADAPTIVE_N0 = [3, 5, 10, 20, 40]     # graph-evidence saturation points to try
R_AT_K = 20
METRIC_NAMES = ("hit", "precision", "recall", "f1", "r@20")


# ---------------------------------------------------------------------------
# Per-query component cache
# ---------------------------------------------------------------------------

class QueryCase:
    __slots__ = ("commit", "flagged", "query", "n_gt", "cand_ids", "gt_mask",
                 "M", "tok_cost", "n_graph")

    def __init__(self, commit, flagged, query, n_gt, cand_ids, gt_mask,
                 M, tok_cost, n_graph):
        self.commit = commit
        self.flagged = flagged
        self.query = query
        self.n_gt = n_gt              # |GT| (may exceed GT present in cands)
        self.cand_ids = cand_ids      # list[str]
        self.gt_mask = gt_mask        # (n_cand,) bool
        self.M = M                    # (n_cand, n_comp) float32
        self.tok_cost = tok_cost      # (n_cand,) int64 estimated tokens
        self.n_graph = n_graph        # graph candidate count (adaptive signal)


def _top_items(d: Dict[str, float], cap: int) -> Dict[str, float]:
    if len(d) <= cap:
        return d
    return dict(sorted(d.items(), key=lambda x: x[1], reverse=True)[:cap])


def build_query_cases(repo_path: str, use_dense: bool) -> Tuple[List[QueryCase], Dict]:
    repo_name = os.path.basename(os.path.abspath(repo_path))
    t0 = time.perf_counter()
    commits = extract_distinct_commits(repo_path)
    symbols = extract_all_symbols(repo_path)
    graph = build_repository_graph(repo_path)
    reverse_graph = build_reverse_graph(graph)
    sset = set(symbols.keys())
    symbol_ids = list(symbols.keys())
    bm25 = BM25Baseline(symbols)
    emb = EmbeddingBaseline(symbols) if use_dense else None
    if use_dense and emb.encoder == "tfidf-cosine-approx":
        print(f"  [{repo_name}] WARNING: dense encoder unavailable; dense leg is tfidf-approx")

    by_file: Dict[str, List[str]] = defaultdict(list)
    for sid in symbol_ids:
        by_file[sid.split(":")[0]].append(sid)

    cases: List[QueryCase] = []
    n_valid_commits = 0
    for c in commits:
        vs = [s for s in c.symbols if s in sset]
        if len(vs) < 2:
            continue
        n_valid_commits += 1
        for q in vs:
            gt = set(vs) - {q}
            g = _top_items(_normalize(_graph_scores(q, graph, reverse_graph)), PER_COMPONENT_CAP)
            q_tokens = _tokenize(symbols[q].code)
            raw = bm25.bm25.get_scores(q_tokens)
            b = _top_items(
                _normalize({sid: raw[i] for i, sid in enumerate(symbol_ids)
                            if sid != q and raw[i] > 0}),
                PER_COMPONENT_CAP)
            f = {sid: 1.0 for sid in by_file[q.split(":")[0]] if sid != q}
            comps = [g, b, f]
            if use_dense and emb._matrix is not None:
                qi = emb._id_to_idx[q]
                sims = emb._matrix @ emb._matrix[qi]
                order = np.argsort(sims)[::-1][:PER_COMPONENT_CAP + 1]
                e = _normalize({emb.symbol_ids[int(i)]: float(sims[int(i)])
                                for i in order
                                if emb.symbol_ids[int(i)] != q and sims[int(i)] > 0})
                comps.append(e)
            elif use_dense:
                comps.append({})

            cand_ids = sorted(set().union(*[set(d) for d in comps]))
            if not cand_ids:
                continue
            idx = {sid: i for i, sid in enumerate(cand_ids)}
            M = np.zeros((len(cand_ids), len(comps)), dtype=np.float32)
            for j, d in enumerate(comps):
                for sid, sc in d.items():
                    M[idx[sid], j] = sc
            gt_mask = np.fromiter((sid in gt for sid in cand_ids),
                                  dtype=bool, count=len(cand_ids))
            tok = np.array([max(1, len(symbols[s].code) // 4) for s in cand_ids],
                           dtype=np.int64)
            cases.append(QueryCase(c.commit_hash, c.flagged_noisy, q, len(gt),
                                   cand_ids, gt_mask, M, tok, len(g)))

    meta = {
        "repo": repo_name, "n_commits": n_valid_commits,
        "n_queries": len(cases),
        "n_symbols": len(symbols),
        "head_sha": os.popen(f"git -C {repo_path} rev-parse HEAD").read().strip(),
        "dense_encoder": (emb.encoder if emb else None),
        "build_seconds": round(time.perf_counter() - t0, 1),
    }
    print(f"  [{repo_name}] {meta['n_commits']} commits, {meta['n_queries']} queries, "
          f"{meta['n_symbols']} symbols in {meta['build_seconds']}s", flush=True)
    return cases, meta


# ---------------------------------------------------------------------------
# Blend evaluation (vectorized over the weight grid)
# ---------------------------------------------------------------------------

def simplex_grid(n_comp: int, step: float) -> np.ndarray:
    """All non-negative weight vectors summing to 1 on a step grid."""
    ticks = int(round(1.0 / step))
    out = []

    def rec(prefix, remaining, depth):
        if depth == n_comp - 1:
            out.append(prefix + [remaining])
            return
        for t in range(remaining + 1):
            rec(prefix + [t], remaining - t, depth + 1)

    rec([], ticks, 0)
    return np.array(out, dtype=np.float32) * step


def _case_metrics(case: QueryCase, ranked: np.ndarray) -> np.ndarray:
    """[hit, precision, recall, f1, r@20] for one ranked candidate index array."""
    if ranked.size == 0:
        return np.zeros(5)
    keep = np.cumsum(case.tok_cost[ranked]) <= TOKEN_BUDGET
    kept = ranked[keep]
    if kept.size == 0:
        return np.zeros(5)
    hits = case.gt_mask[kept]
    tp = int(hits.sum())
    prec = tp / kept.size
    rec = tp / case.n_gt
    f1 = 2 * prec * rec / (prec + rec) if prec + rec > 0 else 0.0
    r20 = int(hits[:R_AT_K].sum()) / case.n_gt
    return np.array([1.0 if tp else 0.0, prec, rec, f1, r20])


def eval_grid_on_cases(cases: List[QueryCase], W: np.ndarray,
                       adaptive_n0: Optional[float] = None) -> Dict[str, np.ndarray]:
    """
    Evaluate every weight vector in W on every case; aggregate per commit.
    If adaptive_n0 is set, the graph weight (component 0) is scaled per query
    by min(1, n_graph / n0) and the deficit redistributed proportionally
    across the remaining components.
    """
    n_combo = W.shape[0]
    per_commit: Dict[str, List[np.ndarray]] = defaultdict(list)
    for case in cases:
        Wq = W
        if adaptive_n0 is not None:
            r = min(1.0, case.n_graph / adaptive_n0)
            Wq = W.copy()
            deficit = Wq[:, 0] * (1.0 - r)
            Wq[:, 0] *= r
            rest = Wq[:, 1:].sum(axis=1)
            safe = rest > 1e-9
            scale = np.ones_like(rest)
            scale[safe] = (rest[safe] + deficit[safe]) / rest[safe]
            Wq[:, 1:] *= scale[:, None]
        S = case.M @ Wq.T                                   # (n_cand, n_combo)
        order = np.argsort(-S, axis=0, kind="stable")[:CANDIDATE_LIMIT]
        vals = np.empty((n_combo, 5))
        for ci in range(n_combo):
            ranked = order[:, ci]
            ranked = ranked[S[ranked, ci] > 1e-12]
            vals[ci] = _case_metrics(case, ranked)
        per_commit[case.commit].append(vals)

    commit_means = [np.mean(v, axis=0) for v in per_commit.values()]
    agg = np.mean(commit_means, axis=0)                     # (n_combo, 5)
    out = {name: agg[:, i] for i, name in enumerate(METRIC_NAMES)}
    out["commit_recall_matrix"] = np.array([cm[:, 2] for cm in commit_means])
    out["commit_ids"] = list(per_commit.keys())
    return out


def eval_cutoff_policies(cases: List[QueryCase], w: np.ndarray) -> Dict[str, Dict]:
    """
    Task: precision. Fixed top-k keeps k symbols no matter what the score
    distribution says; a dynamic cutoff reads the per-query distribution.
    Policies (all followed by the token-budget truncation):
      topk_K       — fixed top-K
      rel_A        — keep while score >= A * peak score (dynamic length)
      gap50        — cut at the largest relative score drop in the top 50
      mass_B       — smallest prefix holding >= B of total score mass
    """
    policies: Dict[str, Dict] = {}
    per_commit: Dict[str, Dict[str, List[np.ndarray]]] = defaultdict(lambda: defaultdict(list))
    counts: Dict[str, List[int]] = defaultdict(list)

    for case in cases:
        s = case.M @ w
        order = np.argsort(-s, kind="stable")[:CANDIDATE_LIMIT]
        order = order[s[order] > 1e-12]
        if order.size == 0:
            continue
        sv = s[order]
        peak = sv[0]
        cuts: Dict[str, np.ndarray] = {}
        for k in (10, 20, 30, 50):
            cuts[f"topk_{k}"] = order[:k]
        for a in (0.05, 0.1, 0.2, 0.3, 0.5):
            cuts[f"rel_{a}"] = order[sv >= a * peak]
        if order.size >= 3:
            head = sv[:50]
            ratios = head[:-1] / np.maximum(head[1:], 1e-12)
            gi = int(np.argmax(ratios)) + 1
            cuts["gap50"] = order[:gi]
        else:
            cuts["gap50"] = order
        total = sv.sum()
        cmass = np.cumsum(sv) / total
        for b in (0.5, 0.7, 0.9):
            cuts[f"mass_{b}"] = order[: int(np.searchsorted(cmass, b) + 1)]
        for pname, ranked in cuts.items():
            per_commit[case.commit][pname].append(_case_metrics(case, ranked))
            counts[pname].append(int(ranked.size))

    all_policies = sorted({p for d in per_commit.values() for p in d})
    for pname in all_policies:
        commit_means = [np.mean(d[pname], axis=0) for d in per_commit.values()
                        if pname in d]
        agg = np.mean(commit_means, axis=0)
        policies[pname] = {
            **{name: round(float(agg[i]), 4) for i, name in enumerate(METRIC_NAMES)},
            "avg_retrieved": round(float(np.mean(counts[pname])), 1),
        }
    return policies


def paired_permutation_p(a: np.ndarray, b: np.ndarray, n_perm: int = 10000,
                         seed: int = 42) -> float:
    """Two-sided sign-flip permutation test on paired per-commit values."""
    d = a - b
    obs = abs(d.mean())
    rng = np.random.default_rng(seed)
    signs = rng.choice([-1.0, 1.0], size=(n_perm, d.size))
    perm = np.abs((signs * d).mean(axis=1))
    return float((np.sum(perm >= obs) + 1) / (n_perm + 1))


def _metrics_at(g: Dict[str, np.ndarray], i: int) -> Dict[str, float]:
    return {k: round(float(g[k][i]), 4) for k in METRIC_NAMES}


# ---------------------------------------------------------------------------
# Main protocols
# ---------------------------------------------------------------------------

def run_loro(repo_cases: Dict[str, List[QueryCase]], metas: Dict[str, Dict],
             use_dense: bool) -> Dict:
    n_comp = 4 if use_dense else 3
    comp_names = ["graph", "bm25", "samefile"] + (["dense"] if use_dense else [])
    W = simplex_grid(n_comp, GRID_STEP)
    print(f"\nGrid: {W.shape[0]} weight vectors over {comp_names}, step {GRID_STEP}",
          flush=True)

    shipped_w = np.array([list(SHIPPED) + ([0.0] if use_dense else [])],
                         dtype=np.float32)

    # one full-grid pass per repo (static blends), reused by every fold
    grid_by_repo: Dict[str, Dict] = {}
    shipped_by_repo: Dict[str, Dict] = {}
    for rname, cases in repo_cases.items():
        t0 = time.perf_counter()
        grid_by_repo[rname] = eval_grid_on_cases(cases, W)
        shipped_by_repo[rname] = eval_grid_on_cases(cases, shipped_w)
        print(f"  grid evaluated on {rname}: {time.perf_counter()-t0:.0f}s", flush=True)

    results = {"components": comp_names, "grid_step": GRID_STEP,
               "objective": "per-commit mean recall",
               "shipped_weights": list(SHIPPED), "folds": {}}

    mean_recall_across = np.mean([grid_by_repo[r]["recall"] for r in repo_cases], axis=0)
    global_best_i = int(np.argmax(mean_recall_across))
    results["global_best_weights"] = [round(float(x), 3) for x in W[global_best_i]]

    for held in repo_cases:
        train = [r for r in repo_cases if r != held]
        if not train:
            print(f"  (skipping LORO fold for {held}: no training repos)")
            continue
        train_recall = np.mean([grid_by_repo[r]["recall"] for r in train], axis=0)
        loro_i = int(np.argmax(train_recall))
        g = grid_by_repo[held]
        self_best_i = int(np.argmax(g["recall"]))

        fold = {
            "loro_selected_weights": [round(float(x), 3) for x in W[loro_i]],
            "self_best_weights": [round(float(x), 3) for x in W[self_best_i]],
            "held_out_metrics": {
                "shipped": _metrics_at(shipped_by_repo[held], 0),
                "loro": _metrics_at(g, loro_i),
                "self_best_oracle": _metrics_at(g, self_best_i),
                "global_best": _metrics_at(g, global_best_i),
            },
            "paired_p_loro_vs_shipped": round(paired_permutation_p(
                g["commit_recall_matrix"][:, loro_i],
                shipped_by_repo[held]["commit_recall_matrix"][:, 0]), 4),
        }
        results["folds"][held] = fold
        hm = fold["held_out_metrics"]
        print(f"\n  LORO fold — held out: {held}")
        print(f"    selected on train: {fold['loro_selected_weights']}"
              f"  (self-best: {fold['self_best_weights']})")
        print(f"    held-out recall: shipped {hm['shipped']['recall']:.3f}"
              f" | loro {hm['loro']['recall']:.3f}"
              f" | oracle {hm['self_best_oracle']['recall']:.3f}"
              f"   (p loro-vs-shipped {fold['paired_p_loro_vs_shipped']:.3f})",
              flush=True)

    # ── adaptive blend ─────────────────────────────────────────────────────
    # Precompute grid passes per (n0, repo) once; folds then only argmax.
    print("\nAdaptive blend (graph weight scaled by per-query graph evidence):",
          flush=True)
    adaptive_grid: Dict[float, Dict[str, Dict]] = {}
    for n0 in ADAPTIVE_N0:
        adaptive_grid[n0] = {}
        t0 = time.perf_counter()
        for rname, cases in repo_cases.items():
            adaptive_grid[n0][rname] = eval_grid_on_cases(cases, W, adaptive_n0=n0)
        print(f"  n0={n0}: grids in {time.perf_counter()-t0:.0f}s", flush=True)

    adaptive = {"n0_grid": ADAPTIVE_N0, "folds": {}}
    for held in repo_cases:
        train = [r for r in repo_cases if r != held]
        if not train:
            print(f"  (skipping adaptive fold for {held}: no training repos)")
            continue
        best = None            # (train_recall, n0, weight_index)
        for n0 in ADAPTIVE_N0:
            tr = np.mean([adaptive_grid[n0][r]["recall"] for r in train], axis=0)
            i = int(np.argmax(tr))
            if best is None or tr[i] > best[0]:
                best = (float(tr[i]), n0, i)
        _, n0, wi = best
        held_eval = adaptive_grid[n0][held]
        static_loro_i = int(np.argmax(
            np.mean([grid_by_repo[r]["recall"] for r in train], axis=0)))
        static_recall = float(grid_by_repo[held]["recall"][static_loro_i])
        p = paired_permutation_p(
            held_eval["commit_recall_matrix"][:, wi],
            grid_by_repo[held]["commit_recall_matrix"][:, static_loro_i])
        adaptive["folds"][held] = {
            "n0": n0,
            "base_weights": [round(float(x), 3) for x in W[wi]],
            "held_out": _metrics_at(held_eval, wi),
            "static_loro_recall": round(static_recall, 4),
            "paired_p_adaptive_vs_static": round(p, 4),
        }
        a = adaptive["folds"][held]
        print(f"  {held}: n0={n0} base={a['base_weights']} "
              f"recall {a['held_out']['recall']:.3f} vs static {a['static_loro_recall']:.3f} "
              f"(p={a['paired_p_adaptive_vs_static']:.3f})", flush=True)
    results["adaptive"] = adaptive

    # ── cutoff policies (precision work), at the shipped weights ───────────
    print("\nCutoff policies (shipped weights), per repo:", flush=True)
    w_ship = np.array(list(SHIPPED) + ([0.0] if use_dense else []),
                      dtype=np.float32)
    results["cutoff_policies"] = {}
    for rname, cases in repo_cases.items():
        pol = eval_cutoff_policies(cases, w_ship)
        results["cutoff_policies"][rname] = pol
        best_f1 = max(pol.items(), key=lambda kv: kv[1]["f1"])
        print(f"  {rname}: best-F1 policy {best_f1[0]} "
              f"(P {best_f1[1]['precision']:.3f} R {best_f1[1]['recall']:.3f} "
              f"F1 {best_f1[1]['f1']:.3f}, avg {best_f1[1]['avg_retrieved']} syms) "
              f"vs topk_20 (P {pol['topk_20']['precision']:.3f} "
              f"R {pol['topk_20']['recall']:.3f} F1 {pol['topk_20']['f1']:.3f})",
              flush=True)

    results["metas"] = metas
    return results


def run_holdout(holdout_cases: Dict[str, List[QueryCase]], metas: Dict[str, Dict],
                frozen_weights: List[float], use_dense: bool) -> Dict:
    """Evaluate frozen weight vectors on repos never used for any selection."""
    shipped_w = np.array([list(SHIPPED) + ([0.0] if use_dense else [])],
                         dtype=np.float32)
    frozen_w = np.array([frozen_weights], dtype=np.float32)
    out = {"frozen_weights": frozen_weights, "repos": {}}
    for rname, cases in holdout_cases.items():
        s = eval_grid_on_cases(cases, shipped_w)
        f = eval_grid_on_cases(cases, frozen_w)
        p = paired_permutation_p(f["commit_recall_matrix"][:, 0],
                                 s["commit_recall_matrix"][:, 0])
        out["repos"][rname] = {
            "n_commits": metas[rname]["n_commits"],
            "n_queries": metas[rname]["n_queries"],
            "shipped": _metrics_at(s, 0),
            "frozen_loro": _metrics_at(f, 0),
            "paired_p": round(p, 4),
        }
        r = out["repos"][rname]
        print(f"  {rname}: shipped recall {r['shipped']['recall']:.3f} "
              f"| frozen {r['frozen_loro']['recall']:.3f} (p={r['paired_p']:.3f})",
              flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dense", action="store_true", help="add dense leg (4-comp grid)")
    ap.add_argument("--holdout", nargs="*", default=None,
                    help="held-out repo names under benchmark_repos/")
    ap.add_argument("--repos", nargs="*", default=None, help="override dev repo list")
    args = ap.parse_args()

    bench = os.path.normpath(os.path.join(os.path.dirname(__file__), "..",
                                          "benchmark_repos"))
    os.makedirs(RESULTS_DIR, exist_ok=True)

    dev = args.repos if args.repos else DEV_REPOS
    print(f"Building component caches for dev repos: {dev}", flush=True)
    repo_cases, metas = {}, {}
    for name in dev:
        path = os.path.join(bench, name)
        if not os.path.isdir(path):
            print(f"  !! missing {path}, skipping")
            continue
        cases, meta = build_query_cases(path, args.dense)
        repo_cases[name] = cases
        metas[name] = meta

    results = run_loro(repo_cases, metas, args.dense)

    if args.holdout:
        # freeze: weights selected on ALL dev repos (global best) — the only
        # legitimate choice for repos outside the dev set
        frozen = results["global_best_weights"]
        print(f"\nHeld-out evaluation with frozen weights {frozen}: {args.holdout}",
              flush=True)
        holdout_cases, hmetas = {}, {}
        for name in args.holdout:
            path = os.path.join(bench, name)
            if not os.path.isdir(path):
                print(f"  !! missing {path}, skipping")
                continue
            cases, meta = build_query_cases(path, args.dense)
            holdout_cases[name] = cases
            hmetas[name] = meta
        results["holdout"] = run_holdout(holdout_cases, hmetas, frozen, args.dense)

    tag = "dense" if args.dense else "3leg"
    out_path = os.path.join(RESULTS_DIR, f"loro_{tag}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nSaved {out_path}")


if __name__ == "__main__":
    main()
