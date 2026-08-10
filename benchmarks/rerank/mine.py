"""
mine.py — build the stage-2 training set.

For every mined commit and every changed symbol `q` in it:

    query      = q alone. The system is NEVER told the other symbols changed;
                 that is exactly what it is being asked to predict.
    positives  = the commit's other changed symbols, if they survive into the
                 stage-1 top-N for q.
    negatives  = the rest of the stage-1 top-N.

Queries whose positives all fall outside the top-N are **dropped and counted**.
That count is the reranker's real ceiling: no reordering of a pool can surface
something the pool does not contain. It is reported, not hidden.

Stage 1 is the product's own `analyze_impact(index, [q])` at shipped defaults,
not a reimplementation, so the pool being reranked is byte-for-byte the pool
that ships.

Output per repo: `benchmarks/results/rerank/<repo>.npz` holding X (float32),
y (uint8), qid (int32 group ids) and a parallel JSON sidecar with the commit
hash, timestamp and candidate ids for each row group — the timestamp is what
makes the temporal split in train.py possible.

    python -m benchmarks.rerank.mine --repos flask click --target 100
"""

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diffcontext.pipeline import index_repository, analyze_impact
from diffcontext.lexical import get_lexical_index
from diffcontext.resolver import build_import_map
from diffcontext.rerank.features import (
    FEATURE_NAMES, N_FEATURES, QueryContext, extract_features,
)
from diffcontext.history import CoChangeIndex

from benchmarks.eval_v2_hardened import extract_distinct_commits

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_REPOS = os.path.join(REPO_ROOT, "benchmark_repos")
OUT_DIR = os.path.join(REPO_ROOT, "benchmarks", "results", "rerank")

# Stage-1 pool depth handed to the reranker. r@100 is ~0.83 and r@200 ~0.93,
# but every extra candidate is a negative the model must reject; 100 is the
# brief's operating point and the one the oracle ceiling was measured at.
POOL_N = 100


def ensure_import_maps(index) -> Optional[Dict[str, Dict[str, str]]]:
    """Import maps for every indexed file, building them if the index came
    from a warm graph cache.

    `index_repository` populates `_import_maps` on a cold index but leaves it
    (and `_file_trees`) None on a graph-cache hit — so mining against a warm
    cache would silently feed the model a constant-zero `import_overlap`.
    That is a real hazard: the feature degrades to "no evidence" exactly as
    designed, so nothing errors and the column just quietly dies.
    """
    if index._import_maps is not None:
        return index._import_maps
    repo_path = index._repo_path
    if not repo_path:
        return None
    maps: Dict[str, Dict[str, str]] = {}
    for rel in {sid.split(":", 1)[0] for sid in index.symbols}:
        abs_path = os.path.join(repo_path, rel[2:] if rel.startswith("./") else rel)
        try:
            maps[rel] = build_import_map(abs_path, repo_path)
        except (SyntaxError, OSError, ValueError):
            maps[rel] = {}
    index._import_maps = maps
    return maps


def commit_timestamps(repo_path: str) -> Dict[str, int]:
    """short-sha -> author timestamp, for the temporal split."""
    try:
        r = subprocess.run(
            ["git", "log", "--format=%H|%ct", "--no-merges"],
            cwd=repo_path, capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    out: Dict[str, int] = {}
    for line in r.stdout.strip().split("\n"):
        if "|" not in line:
            continue
        sha, ts = line.split("|", 1)
        try:
            out[sha[:10]] = int(ts)
        except ValueError:
            continue
    return out


def stage1_pool(index, q: str, n: int = POOL_N) -> List[str]:
    """The shipped stage-1 ranking for query `q`, truncated to `n`.

    Uses the product path at its defaults (hybrid + adaptive), so this is the
    pool the reranker will actually see in production.
    """
    result = analyze_impact(index, [q])
    scores = result.scores
    ranked = sorted(
        ((sid, sc) for sid, sc in scores.items() if sid != q),
        key=lambda kv: (-kv[1], kv[0]),      # deterministic tie-break
    )
    return [sid for sid, _ in ranked[:n]]


def mine_repo(
    repo_name: str,
    target: int = 100,
    pool_n: int = POOL_N,
    with_cochange: bool = True,
) -> Optional[Dict]:
    repo_path = os.path.join(BENCH_REPOS, repo_name)
    if not os.path.isdir(repo_path):
        print(f"  !! {repo_name}: no such repo at {repo_path}")
        return None

    print(f"\n{'='*66}\n  {repo_name}\n{'='*66}")
    t0 = time.perf_counter()
    index = index_repository(repo_path)
    lex = get_lexical_index(index)
    imaps = ensure_import_maps(index)
    print(f"  indexed: {len(index.symbols)} symbols, {index.total_edges} edges, "
          f"import maps for {len(imaps or {})} files "
          f"({time.perf_counter()-t0:.1f}s)")

    commits = extract_distinct_commits(repo_path, target=target)
    ts_map = commit_timestamps(repo_path)
    noisy = sum(c.flagged_noisy for c in commits)
    commits = [c for c in commits if not c.flagged_noisy]
    print(f"  commits mined: {len(commits)} usable (+{noisy} noisy, excluded)")
    if not commits:
        return None

    cochange = None
    if with_cochange:
        t_cc = time.perf_counter()
        # Every evaluated commit is excluded from the history index: the
        # signal must never contain the commit it is scored on.
        cochange = CoChangeIndex(
            repo_path, exclude_commits={c.commit_hash for c in commits},
        )
        print(f"  co-change index built ({time.perf_counter()-t_cc:.1f}s)")

    sset = set(index.symbols)
    file_counts: Dict[str, int] = {}
    for sid in index.symbols:
        f = sid.split(":", 1)[0]
        file_counts[f] = file_counts.get(f, 0) + 1
    token_cache: Dict[str, frozenset] = {}

    X: List[List[float]] = []
    y: List[int] = []
    qid: List[int] = []
    groups: List[Dict] = []

    n_queries = 0
    n_dropped_no_gt = 0          # commit had no other symbol alive at HEAD
    n_dropped_unreachable = 0    # positives exist but none inside the pool
    n_pos_total = 0
    recall_at_pool: List[float] = []

    t_mine = time.perf_counter()
    for c in commits:
        alive = [s for s in c.symbols if s in sset]
        if len(alive) < 2:
            continue
        ts = ts_map.get(c.commit_hash, 0)
        for q in alive:
            gt = set(alive) - {q}
            if not gt:
                n_dropped_no_gt += 1
                continue
            pool = stage1_pool(index, q, pool_n)
            hits = gt & set(pool)
            recall_at_pool.append(len(hits) / len(gt))
            if not hits:
                # Unreachable by ANY reranker over this pool. This is the
                # ceiling, and it is reported.
                n_dropped_unreachable += 1
                continue

            bm = lex.scores_for(index.symbols[q].code)
            bm.pop(q, None)
            # Co-change is keyed off the QUERY's file only. Using the whole
            # commit's file list would tell the model which other files
            # changed — the very thing it is being asked to predict.
            cc_scores = (
                cochange.scores_for_symbols([q]) if cochange is not None else None
            )
            ctx = QueryContext(
                index.symbols, index.graph, index.reverse_graph, [q],
                bm25_scores=bm,
                import_maps=index._import_maps,
                cochange=cc_scores,
                file_counts=file_counts,
                token_cache=token_cache,
            )
            g = n_queries
            for cand in pool:
                X.append(extract_features(ctx, cand))
                y.append(1 if cand in gt else 0)
                qid.append(g)
            n_pos_total += len(hits)
            groups.append({
                "qid": g, "repo": repo_name, "commit": c.commit_hash,
                "commit_ts": ts, "query": q, "n_gt": len(gt),
                "n_gt_in_pool": len(hits), "pool": pool,
            })
            n_queries += 1

    if not X:
        print(f"  !! {repo_name}: no usable queries")
        return None

    Xa = np.asarray(X, dtype=np.float32)
    ya = np.asarray(y, dtype=np.uint8)
    qa = np.asarray(qid, dtype=np.int32)

    os.makedirs(OUT_DIR, exist_ok=True)
    np.savez_compressed(
        os.path.join(OUT_DIR, f"{repo_name}.npz"), X=Xa, y=ya, qid=qa,
    )
    meta = {
        "repo": repo_name,
        "feature_names": list(FEATURE_NAMES),
        "pool_n": pool_n,
        "with_cochange": with_cochange,
        "n_queries": n_queries,
        "n_rows": int(Xa.shape[0]),
        "n_positives": int(ya.sum()),
        "positive_rate": float(ya.mean()),
        "dropped_no_gt": n_dropped_no_gt,
        "dropped_unreachable": n_dropped_unreachable,
        "reachable_frac": n_queries / max(1, n_queries + n_dropped_unreachable),
        "mean_recall_at_pool": float(np.mean(recall_at_pool)) if recall_at_pool else 0.0,
        "groups": groups,
        "mined_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(OUT_DIR, f"{repo_name}.meta.json"), "w") as fh:
        json.dump(meta, fh)

    print(f"  queries kept      : {n_queries}")
    print(f"  dropped (no pos in top-{pool_n}): {n_dropped_unreachable} "
          f"-> CEILING: {meta['reachable_frac']:.1%} of queries are rerankable")
    print(f"  mean r@{pool_n} (stage-1)      : {meta['mean_recall_at_pool']:.3f}")
    print(f"  rows {Xa.shape[0]}  positives {int(ya.sum())} "
          f"({ya.mean():.2%})  [{time.perf_counter()-t_mine:.0f}s]")
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", nargs="+", required=True)
    ap.add_argument("--target", type=int, default=100,
                    help="commits to mine per repo")
    ap.add_argument("--pool-n", type=int, default=POOL_N)
    ap.add_argument("--no-cochange", action="store_true")
    args = ap.parse_args()

    metas = []
    for r in args.repos:
        m = mine_repo(r, args.target, args.pool_n, not args.no_cochange)
        if m:
            metas.append(m)

    if metas:
        print(f"\n{'='*66}\n  SUMMARY\n{'='*66}")
        print(f"  {'repo':12s} {'queries':>8s} {'rows':>8s} {'pos%':>7s} "
              f"{'rerankable':>11s} {'r@pool':>7s}")
        for m in metas:
            print(f"  {m['repo']:12s} {m['n_queries']:8d} {m['n_rows']:8d} "
                  f"{m['positive_rate']:6.2%} {m['reachable_frac']:10.1%} "
                  f"{m['mean_recall_at_pool']:7.3f}")
        print(f"  {'TOTAL':12s} {sum(m['n_queries'] for m in metas):8d} "
              f"{sum(m['n_rows'] for m in metas):8d}")


if __name__ == "__main__":
    main()
