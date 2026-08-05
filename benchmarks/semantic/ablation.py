#!/usr/bin/env python3
"""
ablation.py — three-way retrieval ablation: semantic vs structural vs hybrid.

Same corpus (the repo's HEAD symbols), same query set, only the retriever
changes:
  semantic    CodeXEmbed cosine over cached vectors (embed_symbols.py)
  structural  DiffContext graph-only impact scoring (analyze_impact hybrid=False
              — pure AST/dependency-graph, NO lexical blend)
  hybrid      reciprocal-rank fusion of the two. This is the Item-4 combined
              system; the LEARNED fusion is Item 5.

Two query sets, both symbol-level code-queries:
  general  mined co-change pairs (Item 1)   relevant = co-changed symbols alive@HEAD
  gap      adversarial gap set (Item 3)      relevant = real-edge, low-lexical partners

Reports NDCG@10, MRR and Recall@10 per arm on each set, pooled across repos,
plus a paired bootstrap of hybrid-minus-semantic per metric (the question: does
adding structure to embeddings help, and by how much). On the GAP set keep the
adversarial_gap.py framing: structural recovers real edges ~by construction, so
the honest read is semantic's BLIND SPOT and whether the hybrid closes it — not
"structural won a fair fight".

Needs the embedding cache; run embed_symbols.py first (on Colab/Kaggle GPU).
Everything here is CPU-only and fast (graph ops + one matmul per query).

Usage:
  python -m benchmarks.semantic.ablation benchmark_repos/click benchmark_repos/flask
  python -m benchmarks.semantic.ablation benchmark_repos/*/ --k 10
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Dict, List, Sequence, Set

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diffcontext.pipeline import analyze_impact, index_repository

from benchmarks.semantic.embed_symbols import load_embeddings
from benchmarks.semantic.metrics import mrr, ndcg_at_k, paired_bootstrap, recall_at_k

_HERE = os.path.dirname(os.path.abspath(__file__))
PAIRS_DIR = os.path.join(_HERE, "pairs")
GAP_DIR = os.path.join(_HERE, "gap")
EMB_DIR = os.path.join(_HERE, "embeddings")
RESULTS_DIR = os.path.join(_HERE, "results")
ARMS = ("semantic", "structural", "hybrid")
METRICS = ("ndcg", "mrr", "recall")
RRF_K = 60


# ---- retrievers --------------------------------------------------------------

def rank_semantic(query_id: str, corpus_ids: List[str], matrix: np.ndarray,
                  id2vec: Dict[str, np.ndarray]) -> List[str]:
    """Cosine ranking (vectors are L2-normalized, so cosine == dot)."""
    sims = matrix @ id2vec[query_id]
    order = np.argsort(-sims)
    return [corpus_ids[j] for j in order if corpus_ids[j] != query_id]


def rank_structural(index, query_id: str) -> List[str]:
    """Graph-only impact ranking — the pure AST/dependency-graph arm."""
    res = analyze_impact(index, [query_id], hybrid=False)
    ranked = sorted(res.scores.items(), key=lambda kv: -kv[1])
    return [sid for sid, _ in ranked if sid != query_id]


def rrf(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> List[str]:
    """Reciprocal-rank fusion over several ranked lists."""
    score: Dict[str, float] = defaultdict(float)
    for ranked in rankings:
        for rank, d in enumerate(ranked, 1):
            score[d] += 1.0 / (k + rank)
    return [d for d, _ in sorted(score.items(), key=lambda kv: -kv[1])]


# ---- per-repo evaluation -----------------------------------------------------

def run_repo(index, id2vec: Dict[str, np.ndarray],
             query_relevant: Dict[str, Set[str]], k: int = 10) -> List[dict]:
    """Per-query per-arm metrics for one repo. Only queries and relevant
    symbols that are both alive@HEAD AND embedded are scored (the corpus)."""
    corpus_ids = [i for i in index.symbols if i in id2vec]
    if not corpus_ids:
        return []
    corpus_set = set(corpus_ids)
    matrix = np.stack([id2vec[i] for i in corpus_ids])

    recs: List[dict] = []
    for q, relevant in query_relevant.items():
        if q not in corpus_set:
            continue
        rel = (relevant & corpus_set) - {q}
        if not rel:
            continue
        ranked = {
            "semantic": rank_semantic(q, corpus_ids, matrix, id2vec),
            "structural": rank_structural(index, q),
        }
        ranked["hybrid"] = rrf([ranked["semantic"], ranked["structural"]])
        rec = {"query": q, "n_rel": len(rel)}
        for arm in ARMS:
            r = ranked[arm]
            rec[arm] = {"ndcg": ndcg_at_k(r, rel, k), "mrr": mrr(r, rel),
                        "recall": recall_at_k(r, rel, k)}
        recs.append(rec)
    return recs


# ---- query sets --------------------------------------------------------------

def load_general(path: str) -> Dict[str, Set[str]]:
    """Item-1 co-change: query -> union of co-changed gt symbols."""
    qr: Dict[str, Set[str]] = defaultdict(set)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    p = json.loads(ln)
                    qr[p["query_symbol"]].update(p["gt_symbols"])
    return qr


def load_gap(path: str) -> Dict[str, Set[str]]:
    """Item-3 adversarial gap: query -> real-edge low-lexical partners."""
    qr: Dict[str, Set[str]] = defaultdict(set)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    g = json.loads(ln)
                    qr[g["query_symbol"]].add(g["gt_symbol"])
    return qr


# ---- reporting ---------------------------------------------------------------

def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def report_set(name: str, recs: List[dict], k: int) -> dict:
    print(f"===== {name} set — {len(recs)} queries (pooled) =====")
    if not recs:
        print("no scorable queries (missing embeddings? run embed_symbols.py)\n")
        return {}
    means = {a: {m: _mean([r[a][m] for r in recs]) for m in METRICS} for a in ARMS}
    print(f"{'arm':11s} {'NDCG@'+str(k):>8s} {'MRR':>8s} {'Recall@'+str(k):>9s}")
    for a in ARMS:
        print(f"{a:11s} {means[a]['ndcg']:8.3f} {means[a]['mrr']:8.3f} {means[a]['recall']:9.3f}")

    print("\npaired bootstrap  hybrid - semantic  (positive => hybrid better; "
          "CI excluding 0 = significant):")
    boot = {}
    for m in METRICS:
        a = [r["semantic"][m] for r in recs]
        b = [r["hybrid"][m] for r in recs]
        bs = paired_bootstrap(a, b)
        boot[m] = bs
        sig = "" if bs["ci_lo"] <= 0 <= bs["ci_hi"] else "  *"
        print(f"  {m:8s} delta={bs['mean_diff']:+.3f}  "
              f"95% CI [{bs['ci_lo']:+.3f}, {bs['ci_hi']:+.3f}]  p={bs['p']:.4f}{sig}")
    print()
    return {"n": len(recs), "means": means, "bootstrap_hybrid_vs_semantic": boot}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="+", help="paths to git repo clones")
    ap.add_argument("--k", type=int, default=10, help="cutoff for @k metrics (default 10)")
    ap.add_argument("--pairs-dir", default=PAIRS_DIR)
    ap.add_argument("--gap-dir", default=GAP_DIR)
    ap.add_argument("--emb-dir", default=EMB_DIR)
    ap.add_argument("--out", default=os.path.join(RESULTS_DIR, "ablation.json"))
    args = ap.parse_args()

    pooled: Dict[str, List[dict]] = {"general": [], "gap": []}
    missing = []
    for r in args.repos:
        name = os.path.basename(os.path.abspath(r).rstrip("/"))
        id2vec, meta = load_embeddings(os.path.join(args.emb_dir, name + ".npz"))
        if not id2vec:
            missing.append(name)
            continue
        index = index_repository(os.path.abspath(r))
        gen_q = load_general(os.path.join(args.pairs_dir, name + ".jsonl"))
        gap_q = load_gap(os.path.join(args.gap_dir, name + ".jsonl"))
        gen_recs = run_repo(index, id2vec, gen_q, args.k)
        gap_recs = run_repo(index, id2vec, gap_q, args.k)
        pooled["general"].extend(gen_recs)
        pooled["gap"].extend(gap_recs)

        # Embedding coverage is the tripwire for a drifted corpus: vectors
        # are keyed by symbol id at a specific HEAD, so a repo checked out at the
        # wrong commit joins partially and quietly shrinks the scored set instead
        # of failing. Anything well under 100% means re-check the pins
        # (benchmarks/semantic/pin_repos.py --check) before reading the metrics.
        n_sym = len(index.symbols)
        covered = sum(1 for s in index.symbols if s in id2vec)
        cov = covered / n_sym if n_sym else 0.0
        flag = "" if cov >= 0.99 else "   <-- PARTIAL JOIN, check pin_repos.py --check"
        print(f"scored {name}: {meta.get('model', '?')} dim={meta.get('dim')} | "
              f"symbols embedded {covered}/{n_sym} ({cov:.0%}){flag}")
        print(f"    queries scored: general {len(gen_recs)}/{len(gen_q)}, "
              f"gap {len(gap_recs)}/{len(gap_q)}")
    if missing:
        print(f"\n!! no embeddings for {', '.join(missing)} — run embed_symbols.py "
              f"for these (Colab/Kaggle GPU), then re-run.\n")
    if len(missing) == len(args.repos):
        # Every repo was skipped: there is nothing to summarize. Fail loudly
        # instead of writing an empty-but-well-formed summary that a sweep
        # script would read as a successful run.
        sys.exit("no embeddings for ANY requested repo — nothing was scored. "
                 "Run embed_symbols.py first; no summary written.")
    print()

    summary = {}
    for setname in ("general", "gap"):
        summary[setname] = report_set(setname, pooled[setname], args.k)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"k": args.k, "repos": [os.path.basename(os.path.abspath(r).rstrip("/"))
                                          for r in args.repos], "sets": summary}, f, indent=2)
    print(f"summary -> {os.path.relpath(args.out)}")


if __name__ == "__main__":
    main()
