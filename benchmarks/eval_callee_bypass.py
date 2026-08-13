#!/usr/bin/env python3
"""
eval_callee_bypass.py — what the cross-file neighbour bypass actually buys.

The bypass moves up to `neighbour_cap` cross-file direct call-graph
neighbours to the front of the selection queue (ordering only — they still
pay the token budget and still count against top_k). It targets the
`cross-file neighbour` population: ~3% of ground truth, structurally
certain, and routinely buried by same-file and lexical noise.

Reports recall broken out by relation group, plus precision_lb and package
size, at several caps. The cap sweep is the point: this is a structural
intervention on a small population, so the question is not just "does 5
help" but "is 5 sitting on a cliff".

`fired` = mean promoted symbols per case. It bounds the blast radius: if
the bypass fires on 0.2 symbols/case it cannot be responsible for a large
swing in anything, in either direction.

Usage:
    python benchmarks/eval_callee_bypass.py                    # frozen
    python benchmarks/eval_callee_bypass.py --repos all --caps 0 3 5 7
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diffcontext.context.selector import cross_file_neighbours
from diffcontext.pipeline import index_repository, analyze_impact
from diffcontext.pipeline import compile as dc_compile
from diffcontext.verify import cases as vcases
from benchmarks.significance import wilcoxon_signed_rank

BENCH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmark_repos"
)
TRAIN = ["django", "click", "flask", "httpx", "pydantic"]
FROZEN = ["black", "requests", "rich", "starlette"]
BUDGET = 10000
GROUPS = ("same-file", "cross-file neighbour", "cross-file other")


def classify(idx, changed, gt):
    csyms = [c for c in changed if c in idx.symbols]
    cfiles = {idx.symbols[c].file for c in csyms}
    gfile = idx.symbols[gt].file if gt in idx.symbols else None
    if gfile in cfiles:
        return "same-file"
    for c in csyms:
        if gt in idx.graph.get(c, []) or c in idx.graph.get(gt, []):
            return "cross-file neighbour"
    return "cross-file other"


def run(repos, caps, ncases):
    tot = {c: {"rec": 0.0, "n": 0, "prec": 0.0, "pn": 0, "sel": 0, "tok": 0,
               "fired": 0, "per_case": [], **{g: [0, 0] for g in GROUPS}}
           for c in caps}

    for name in repos:
        path = os.path.join(BENCH, name)
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        idx = index_repository(path)
        cases = vcases.cases_from_history(path, max_cases=ncases,
                                          known_symbols=set(idx.symbols))
        if len(cases) < 5:
            continue
        t = time.perf_counter()
        # Keyed by index, not case.name: mined names collide when one commit
        # touches two symbols sharing a trailing name.
        groups = [{g: classify(idx, c.changed, g) for g in c.must_include}
                  for c in cases]

        for cap in caps:
            for ci, case in enumerate(cases):
                imp = analyze_impact(idx, case.changed, max_depth=case.depth)
                tk = case.top_k * len(case.changed) if case.top_k > 0 else None
                pkg = dc_compile(idx, imp, max_tokens=BUDGET, top_k=tk,
                                 neighbour_cap=cap)
                sel = {it.symbol_id for it in pkg.items}
                gt = case.must_include
                d = tot[cap]
                case_rec = len(set(gt) & sel) / len(gt) if gt else 0.0
                d["rec"] += case_rec
                d["per_case"].append(case_rec)
                d["n"] += 1
                d["sel"] += len(sel)
                d["tok"] += pkg.token_estimate
                if cap:
                    nb = cross_file_neighbours(case.changed, idx.symbols,
                                               idx.graph, idx.reverse_graph)
                    d["fired"] += min(len(nb & set(imp.scores)), cap)
                nonchanged = sel - set(case.changed)
                if nonchanged:
                    d["prec"] += len(set(gt) & nonchanged) / len(nonchanged)
                    d["pn"] += 1
                for g in gt:
                    grp = groups[ci][g]
                    d[grp][1] += 1
                    if g in sel:
                        d[grp][0] += 1
        print(f"  [{name}] {len(cases)} cases x {len(caps)} caps "
              f"({time.perf_counter() - t:.0f}s)", flush=True)

    print(f"\n=== callee bypass  (<={ncases} cases/repo, budget {BUDGET:,}) ===")
    hdr = (f"  {'cap':>4} {'recall':>7} {'prec_lb':>8} {'n_sel':>6} "
           f"{'tokens':>7} {'fired':>6}")
    for g in GROUPS:
        hdr += f" {g:>22}"
    print(hdr)
    base = None
    for cap in caps:
        d = tot[cap]
        if not d["n"]:
            continue
        row = (f"  {cap:>4} {d['rec'] / d['n'] * 100:>6.1f}% "
               f"{d['prec'] / d['pn'] * 100 if d['pn'] else 0:>7.1f}% "
               f"{d['sel'] / d['n']:>6.1f} {d['tok'] / d['n']:>7.0f} "
               f"{d['fired'] / d['n']:>6.2f}")
        for g in GROUPS:
            hit, n = d[g]
            row += f" {hit / n * 100 if n else 0:>21.1f}%"
        print(row)
        if base is None:
            base = d
    if base is not None and len(caps) > 1:
        print(f"\n  deltas vs cap={caps[0]}:")
        for cap in caps[1:]:
            d = tot[cap]
            if not d["n"]:
                continue
            dr = (d["rec"] / d["n"] - base["rec"] / base["n"]) * 100
            dp = ((d["prec"] / d["pn"] if d["pn"] else 0)
                  - (base["prec"] / base["pn"] if base["pn"] else 0)) * 100
            parts = []
            for g in GROUPS:
                h1, n1 = d[g]
                h0, n0 = base[g]
                parts.append(f"{g} {(h1 / n1 - h0 / n0) * 100 if n1 else 0:+.1f}pp")
            print(f"    cap={cap}: recall {dr:+.2f}pp  prec_lb {dp:+.2f}pp  | "
                  + "  ".join(parts))
    print("\n  n per group: " + "  ".join(
        f"{g}={tot[caps[0]][g][1]}" for g in GROUPS))

    # Paired bootstrap on per-case recall. The arms run the same cases through
    # the same pipeline and differ only in the selection rule, so pairing
    # removes case difficulty entirely; what is left is whether the mean
    # improvement would survive a different draw of repos/cases. A headline
    # of +0.5pp on a 3%-of-GT population needs this stated, not assumed.
    #
    # Both tests are printed because they disagree at cap=7, and the
    # disagreement is the finding. Wilcoxon ranks the changed pairs and asks
    # whether more cases improve than worsen -- yes at every cap. The
    # bootstrap CI is on the MEAN, which is magnitude-sensitive, and at cap=7
    # the cases the bypass hurts are big enough to pull it back across zero.
    # Reporting only Wilcoxon would sell cap=7 as a win it is not.
    if base is not None and len(caps) > 1:
        import random
        print("\n  paired bootstrap on per-case recall (10k resamples):")
        b0 = tot[caps[0]]["per_case"]
        for cap in caps[1:]:
            b1 = tot[cap]["per_case"]
            if len(b1) != len(b0):
                continue
            deltas = [a - b for a, b in zip(b1, b0)]
            n = len(deltas)
            rng = random.Random(0)
            means = sorted(
                sum(rng.choices(deltas, k=n)) / n for _ in range(10000)
            )
            lo, hi = means[250] * 100, means[9750] * 100
            changed_n = sum(1 for d in deltas if d != 0)
            sig = "excludes 0" if lo > 0 or hi < 0 else "INCLUDES 0"
            _, p, _ = wilcoxon_signed_rank(b1, b0)
            print(f"    cap={cap}: {sum(deltas) / n * 100:+.2f}pp  "
                  f"95% CI [{lo:+.2f}, {hi:+.2f}]  {sig}  "
                  f"wilcoxon p={p:.4f}  ({changed_n}/{n} cases changed)")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--repos", choices=["frozen", "train", "all"], default="frozen")
    ap.add_argument("--caps", type=int, nargs="+", default=[0, 3, 5, 7])
    ap.add_argument("--cases", type=int, default=150)
    args = ap.parse_args()
    repos = {"frozen": FROZEN, "train": TRAIN, "all": TRAIN + FROZEN}[args.repos]
    run(repos, args.caps, args.cases)
