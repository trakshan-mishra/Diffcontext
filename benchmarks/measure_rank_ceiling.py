"""
Counterfactual oracle rerank: how much recall is theoretically recoverable by
RANKING ALONE, holding the candidate pool and the budget fixed?

The oracle knows which candidates are ground truth and floats them above every
non-GT candidate, preserving relative order within each group. It cannot add
symbols -- it only reorders the stage-1 pool. So the gap between the oracle and
the shipped ranker is ranking headroom; whatever the oracle itself cannot reach
is imposed by something else, and the arms separate which:

  oracle top_k=20      the shipped operating point
  oracle top_k=off     removes the cutoff, budget still binds  -> isolates top_k
  oracle unbounded     no cutoff, no budget                    -> validates that
                       pool-miss really is 0; should be ~100%

`fit ceiling` is the fraction of GT that could fit if we packed GT and nothing
else -- the hard budget ceiling, independent of any ranker.
"""

import os
import sys
from dataclasses import replace

sys.path.insert(0, "/home/trakshan/temporary/titanic.csv/diff/Diffcontext")

from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile
from diffcontext.verify import cases as vcases
from diffcontext.context.selector import _estimate_tokens

BENCH = "/home/trakshan/temporary/titanic.csv/diff/Diffcontext/benchmark_repos"
TRAIN = ["django", "click", "flask", "httpx", "pydantic"]
FROZEN = ["black", "requests", "rich", "starlette"]
NCASES = int(sys.argv[1]) if len(sys.argv) > 1 else 150
BUDGET = 10000


def oracle_scores(scores, gt):
    """Float GT above all non-GT, preserving order inside each group."""
    if not scores:
        return dict(scores)
    mx = max(scores.values())
    return {sid: (sc + mx + 1.0 if sid in gt else sc) for sid, sc in scores.items()}


def recall_of(selected, gt):
    return len(set(gt) & selected) / len(gt) if gt else 0.0


def run_repo(path):
    idx = index_repository(path)
    cases = vcases.cases_from_history(path, max_cases=NCASES,
                                      known_symbols=set(idx.symbols))
    if len(cases) < 5:
        return None

    acc = {k: [0.0, 0] for k in
           ("stage1", "rerank", "oracle_tk20", "oracle_notk", "oracle_unbounded",
            "fit_ceiling")}
    not_in_pool = 0
    gt_total = 0

    for case in cases:
        gt = [g for g in case.must_include]
        gt_total += len(gt)
        top_k = case.top_k * len(case.changed) if case.top_k > 0 else None

        for label, rr in (("stage1", False), ("rerank", True)):
            imp = analyze_impact(idx, case.changed, max_depth=case.depth, rerank=rr)
            pkg = dc_compile(idx, imp, max_tokens=BUDGET, top_k=top_k)
            sel = {it.symbol_id for it in pkg.items}
            acc[label][0] += recall_of(sel, gt)
            acc[label][1] += 1
            if label == "stage1":
                base = imp

        not_in_pool += sum(1 for g in gt if g not in base.scores)
        osc = oracle_scores(base.scores, set(gt))
        oimp = replace(base, scores=osc)

        for label, tk, bud in (("oracle_tk20", top_k, BUDGET),
                               ("oracle_notk", None, BUDGET),
                               ("oracle_unbounded", None, None)):
            pkg = dc_compile(idx, oimp, max_tokens=bud, top_k=tk)
            sel = {it.symbol_id for it in pkg.items}
            acc[label][0] += recall_of(sel, gt)
            acc[label][1] += 1

        # Hard budget ceiling: greedily pack GT only, cheapest first.
        sizes = sorted(_estimate_tokens(idx.symbols[g].code)
                       for g in gt if g in idx.symbols)
        used, fit = 0, 0
        for s in sizes:
            if used + s > BUDGET:
                break
            used += s
            fit += 1
        acc["fit_ceiling"][0] += (fit / len(gt)) if gt else 0.0
        acc["fit_ceiling"][1] += 1

    return acc, not_in_pool, gt_total


def report(title, repos):
    tot = {}
    nip = gtt = 0
    for name in repos:
        path = os.path.join(BENCH, name)
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        out = run_repo(path)
        if out is None:
            print(f"  skip {name}", file=sys.stderr)
            continue
        acc, n, g = out
        nip += n
        gtt += g
        for k, (s, c) in acc.items():
            p = tot.setdefault(k, [0.0, 0])
            p[0] += s
            p[1] += c

    if not tot:
        return
    print(f"\n=== {title} ===")
    print(f"  GT symbols not in stage-1 pool: {nip} / {gtt}")
    order = ["stage1", "rerank", "oracle_tk20", "oracle_notk",
             "oracle_unbounded", "fit_ceiling"]
    for k in order:
        s, c = tot[k]
        print(f"  {k:<18} {s / c * 100:>6.1f}%")


if __name__ == "__main__":
    report(f"ALL 9 REPOS  (cases<={NCASES}/repo, budget={BUDGET:,})", TRAIN + FROZEN)
    report(f"FROZEN ONLY  (cases<={NCASES}/repo, budget={BUDGET:,})", FROZEN)
