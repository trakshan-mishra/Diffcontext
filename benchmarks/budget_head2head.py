#!/usr/bin/env python3
"""
budget_head2head.py — Is DiffContext actually better than plain grep under a
tight token budget, or just fancier?

Head-to-head at IDENTICAL token budgets, on real co-change ground truth:

  Method A (grep-packing) — what a developer or naive agent does: paste the
    changed function, then grep the repo for `name(` and paste every matching
    function, in file order, until the window is full.
  Method B (diffcontext)  — `compile(max_tokens=budget, top_k=20)`.

Ground truth: distinct commits mined from the target repo's git history
where >=3 functions changed together (same mining as eval_v2). Query with
one changed function; ground truth = the other functions from that commit.
Score = fraction of true co-change partners inside the packed context.

Also reports an HONESTY AUDIT at the tightest realistic budget: for every
ground-truth symbol DiffContext missed, was it disclosed in the dropped
manifest, or silently invisible? (A retrieval tool that hides what it cut
is worse than useless in an agent loop.)

Usage:
  python benchmarks/budget_head2head.py benchmark_repos/black
  python benchmarks/budget_head2head.py <any repo with git history>
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.eval_v2_hardened import extract_distinct_commits
from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile

BUDGETS = [1000, 2000, 4000, 8000]
AUDIT_BUDGET = 2000
EST = lambda t: max(1, len(t) // 4)   # same token heuristic for both methods


def grep_baseline(symbols, query_id, budget):
    """Paste the changed function, then grep-matches in file order, until full."""
    name = query_id.split(":")[1].split(".")[-1]
    packed, used = [], 0

    def try_add(sid):
        nonlocal used
        cost = EST(symbols[sid].code)
        if used + cost <= budget:
            packed.append(sid)
            used += cost

    try_add(query_id)
    matches = sorted(
        (sid for sid, s in symbols.items()
         if sid != query_id and (name + "(") in s.code),
        key=lambda sid: (sid.split(":")[0], symbols[sid].lineno or 0),
    )
    for sid in matches:
        try_add(sid)
    return packed


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "..", "benchmark_repos", "black")
    repo = os.path.normpath(repo)

    print(f"mining co-change commits from {os.path.basename(repo)} ...")
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

    agg = {b: {"grep": [], "dc": []} for b in BUDGETS}
    honesty = {"in_context": 0, "disclosed_dropped": 0, "invisible": 0}

    for q, gt in cases:
        impact = analyze_impact(idx, [q])
        for b in BUDGETS:
            g_packed = grep_baseline(idx.symbols, q, b)
            agg[b]["grep"].append(len(set(g_packed) & gt) / len(gt))

            ctx = dc_compile(idx, impact, max_tokens=b, top_k=20)
            d_packed = {it.symbol_id for it in ctx.items}
            agg[b]["dc"].append(len(d_packed & gt) / len(gt))

            if b == AUDIT_BUDGET:
                dropped = set(ctx.dropped_symbols)
                for g in gt:
                    if g in d_packed:
                        honesty["in_context"] += 1
                    elif g in dropped:
                        honesty["disclosed_dropped"] += 1
                    else:
                        honesty["invisible"] += 1

    print(f"{'budget':>8} {'grep recall':>12} {'diffcontext':>12} {'delta':>8}")
    for b in BUDGETS:
        g = sum(agg[b]["grep"]) / len(agg[b]["grep"])
        d = sum(agg[b]["dc"]) / len(agg[b]["dc"])
        print(f"{b:>8} {g:>12.3f} {d:>12.3f} {d - g:>+8.3f}")

    n = sum(honesty.values())
    print(f"\nHonesty audit @{AUDIT_BUDGET} tokens ({n} ground-truth symbols):")
    for k, v in honesty.items():
        print(f"  {k:<18} {v:>4}  ({v / n:.1%})")


if __name__ == "__main__":
    main()
