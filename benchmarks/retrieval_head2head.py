#!/usr/bin/env python3
"""
retrieval_head2head.py — one fair table, four retrievers, identical cases.

Answers "is the 4/10 retrieval score real, or a benchmark artifact?" by
scoring every method on the SAME mined co-change cases at the SAME token
budget, and reporting BOTH recall and precision (the gap-cutoff story is a
precision story, so a recall-only table hides the point).

  bm25         rank-BM25 over full function sources (the standard IR baseline)
  grep         paste the changed fn, then every `name(` match in file order
  diffcontext  hybrid retrieval, top-k=20 (recall-first default)
  dc_gap       hybrid retrieval + largest-gap cutoff (precision operating point)

Ground truth is identical to eval_v2 / budget_head2head: distinct commits
from the repo's own git history where >=3 functions changed together; query
with one changed function, ground truth = the other functions in that commit.
Every method is capped by the same token budget so set sizes are comparable.

Usage:
  python benchmarks/retrieval_head2head.py benchmark_repos/black
  python benchmarks/retrieval_head2head.py benchmark_repos/flask 8000
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.eval_v2_hardened import extract_distinct_commits
from benchmarks.baselines import BM25Baseline
from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile

EST = lambda t: max(1, len(t) // 4)   # same ~4-chars/token heuristic for all arms


def pack(sids, symbols, budget):
    """Greedily keep ranked sids until the token budget is spent (rank order)."""
    out, used = [], 0
    for sid in sids:
        cost = EST(symbols[sid].code)
        if used + cost <= budget:
            out.append(sid)
            used += cost
    return out


def grep_matches(symbols, query_id):
    """Every function whose source contains `name(`, in file/line order."""
    name = query_id.split(":")[1].split(".")[-1]
    return sorted(
        (sid for sid, s in symbols.items()
         if sid != query_id and (name + "(") in s.code),
        key=lambda sid: (sid.split(":")[0], symbols[sid].lineno or 0),
    )


def score(retrieved, gt, query_id):
    """recall, precision, size — query excluded from the retrieved set."""
    r = set(retrieved) - {query_id}
    hits = len(r & gt)
    recall = hits / len(gt) if gt else 0.0
    precision = hits / len(r) if r else 0.0
    return recall, precision, len(r)


def main():
    repo = os.path.normpath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "..", "benchmark_repos", "black")
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 8000

    print(f"mining co-change commits from {os.path.basename(repo)} "
          f"(budget={budget} tokens) ...")
    t0 = time.time()
    commits = extract_distinct_commits(repo, target=60, scan_limit=3000)
    idx = index_repository(repo)
    sset = set(idx.symbols)

    cases = []
    for c in commits:
        alive = [s for s in c.symbols if s in sset]
        if len(alive) >= 3 and not c.flagged_noisy:
            for q in alive[:2]:
                cases.append((q, set(alive) - {q}))
    cases = cases[:30]
    print(f"  {len(cases)} query cases, setup {time.time() - t0:.0f}s\n")

    bm25 = BM25Baseline(idx.symbols)               # built once over the repo
    methods = ["bm25", "grep", "diffcontext", "dc_gap"]
    agg = {m: {"recall": [], "precision": [], "size": []} for m in methods}

    for q, gt in cases:
        impact = analyze_impact(idx, [q])
        selected = {
            "bm25": pack(bm25.retrieve(q, top_k=50), idx.symbols, budget),
            "grep": pack(grep_matches(idx.symbols, q), idx.symbols, budget),
            "diffcontext": [it.symbol_id for it in
                            dc_compile(idx, impact, max_tokens=budget, top_k=20).items],
            "dc_gap": [it.symbol_id for it in
                       dc_compile(idx, impact, max_tokens=budget, cutoff="gap").items],
        }
        for m in methods:
            rec, prec, size = score(selected[m], gt, q)
            agg[m]["recall"].append(rec)
            agg[m]["precision"].append(prec)
            agg[m]["size"].append(size)

    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    print(f"{'method':>12} {'recall':>8} {'precision':>10} {'F1':>7} {'avg #syms':>10}")
    print("-" * 52)
    for m in methods:
        r, p = mean(agg[m]["recall"]), mean(agg[m]["precision"])
        f1 = 2 * r * p / (r + p) if (r + p) else 0.0
        print(f"{m:>12} {r:>8.3f} {p:>10.3f} {f1:>7.3f} {mean(agg[m]['size']):>10.1f}")

    print(f"\n{len(cases)} cases, budget {budget} tok. "
          f"recall = fraction of true co-change partners retrieved; "
          f"precision = fraction of retrieved symbols that were partners.")


if __name__ == "__main__":
    main()
