#!/usr/bin/env python3
"""
history_signal_sweep.py — is `cross-file other` recall a signal-DESIGN
problem or a signal-AVAILABILITY problem?

Context. Breaking retrieval misses down by how the ground-truth symbol
relates to the changed one gives three populations:

    same-file             GT shares the changed symbol's file
    cross-file neighbour  direct call edge, different file
    cross-file other      no edge, no co-location  <- 41% of all GT, ~13% recall

The weight sweep showed same-file and cross-file-neighbour have opposite
optima, so no global scalar serves both. But every one of those runs had
CoChangeIndex OFF — the one signal designed for `cross-file other`. This
script turns it on and asks whether that population is simply unreachable
without it.

Answer (frozen repos, 150 cases each): NO. The signal is available and it
still does not help. See the two diagnostics below, which are the point of
this script — the recall table alone would be misread as "history is
useless", when what is actually true is "history is reachable but not
discriminative at file granularity".

    hist-reach   fraction of GT whose FILE has nonzero honest co-change
                 association with a changed file. This is history's recall
                 CEILING: it cannot retrieve what it cannot see, at any
                 weight. Measured ~80% for cross-file other.

    --precision  files lit per case vs. how many are GT, swept over
                 min_cochanges. Measured 4-17% precision: history lights
                 20-45 files to reach ~2 GT ones, and thresholding trades
                 reach away ~2x faster than it buys precision.

Because association medians are ~0.05-0.15, `history_weight=0.15` moves a
GT symbol about 1-2 points on the 0-100 blend scale, against a flat 20 for
same-file. That is the mechanism: the signal is real, an order of magnitude
too diffuse to reorder anything, and raising its weight costs same-file
recall faster than it buys cross-file-other recall.

LEAKAGE. Cases are mined FROM git history, so a CoChangeIndex over that
same history has already seen the evaluated commit — and that commit's own
co-change pairs ARE the ground truth. `exclude_commits` prevents this; it
was silently a no-op until f0c7ead. The `leaky` arm keeps the old behavior
deliberately, so the gap between it and `honest` stays visible rather than
being something a future reader has to take on trust.

Usage:
    python benchmarks/history_signal_sweep.py                 # frozen repos
    python benchmarks/history_signal_sweep.py --repos all
    python benchmarks/history_signal_sweep.py --precision     # threshold sweep
"""

import argparse
import inspect
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diffcontext.pipeline as P
from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile
from diffcontext.history import CoChangeIndex
from diffcontext.verify import cases as vcases

BENCH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmark_repos"
)
# Same split the reranker uses, so numbers here are comparable to it.
TRAIN = ["django", "click", "flask", "httpx", "pydantic"]
FROZEN = ["black", "requests", "rich", "starlette"]
BUDGET = 10000

# (label, co-change index kind, history_weight)
ARMS = [
    ("baseline (no history)", None,    0.0),
    ("history honest w=.15", "honest", 0.15),
    ("history honest w=.50", "honest", 0.50),
    ("history LEAKY  w=.15", "leaky",  0.15),
]
GROUPS = ("same-file", "cross-file neighbour", "cross-file other")


def patch_default(fn, name, value):
    """Rebind a default arg by NAME.

    _blend_hybrid binds `weights` and `history_weight` as default arguments
    at import time and analyze_impact never passes them, so rebinding the
    module attribute does nothing. Patch by name rather than by index so
    this keeps working if the signature gains a parameter.
    """
    params = [p for p in inspect.signature(fn).parameters.values()
              if p.default is not inspect.Parameter.empty]
    i = [p.name for p in params].index(name)
    d = list(fn.__defaults__)
    d[i] = value
    fn.__defaults__ = tuple(d)
    assert fn.__defaults__[i] == value, f"{name} patch did not take"


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


def load_repo(name, ncases):
    """(index, cases, eval_commit_hashes) or None if unusable."""
    path = os.path.join(BENCH, name)
    if not os.path.isdir(os.path.join(path, ".git")):
        return None
    idx = index_repository(path)
    cases = vcases.cases_from_history(
        path, max_cases=ncases, known_symbols=set(idx.symbols)
    )
    if len(cases) < 5:
        return None
    # Mined case names are "history-<hash>-<symbol>", so the evaluated
    # commit is recoverable from the case itself — no second mining pass
    # that could drift out of sync with what is actually being scored.
    return path, idx, cases, {c.name.split("-")[1] for c in cases}


def run(repos, title, ncases):
    tot = {lab: {"rec": 0.0, "n": 0, "prec": 0.0, "pn": 0, "sel": 0,
                 **{g: [0, 0] for g in GROUPS}} for lab, _, _ in ARMS}
    reach = {g: [0, 0] for g in GROUPS}

    for name in repos:
        loaded = load_repo(name, ncases)
        if not loaded:
            continue
        path, idx, cases, eval_commits = loaded

        t = time.perf_counter()
        honest = CoChangeIndex(path, exclude_commits=eval_commits)
        leaky = CoChangeIndex(path)
        # Assert the control engaged. The bug this script was written around
        # was a leakage guard that reported success while doing nothing.
        assert honest.excluded_commits > 0, f"{name}: exclusion matched nothing"
        print(f"  [{name}] {len(cases)} cases | co-change mined "
              f"{honest.mined_commits} honest / {leaky.mined_commits} leaky "
              f"({honest.excluded_commits} eval commits dropped) "
              f"{time.perf_counter() - t:.0f}s", flush=True)
        hist = {"honest": honest, "leaky": leaky}

        # Keyed by INDEX, not case.name: mined names collide when one commit
        # touches two symbols with the same trailing name, which would mix up
        # whose ground truth is whose.
        groups = [{g: classify(idx, c.changed, g) for g in c.must_include}
                  for c in cases]

        for ci, case in enumerate(cases):
            hs = honest.scores_for_symbols(case.changed)
            for g in case.must_include:
                grp = groups[ci][g]
                reach[grp][1] += 1
                if hs.get(g.split(":")[0], 0.0) > 0.0:
                    reach[grp][0] += 1

        for lab, kind, hw in ARMS:
            patch_default(P._blend_hybrid, "history_weight", hw)
            hidx = hist[kind] if kind else None
            for ci, case in enumerate(cases):
                imp = analyze_impact(idx, case.changed, max_depth=case.depth,
                                     history=hidx)
                tk = case.top_k * len(case.changed) if case.top_k > 0 else None
                pkg = dc_compile(idx, imp, max_tokens=BUDGET, top_k=tk)
                sel = {it.symbol_id for it in pkg.items}
                gt = case.must_include
                tot[lab]["rec"] += len(set(gt) & sel) / len(gt) if gt else 0.0
                tot[lab]["n"] += 1
                tot[lab]["sel"] += len(sel)
                nonchanged = sel - set(case.changed)
                if nonchanged:
                    tot[lab]["prec"] += len(set(gt) & nonchanged) / len(nonchanged)
                    tot[lab]["pn"] += 1
                for g in gt:
                    grp = groups[ci][g]
                    tot[lab][grp][1] += 1
                    if g in sel:
                        tot[lab][grp][0] += 1

    print(f"\n=== {title}  (<={ncases} cases/repo, budget {BUDGET:,}) ===")
    hdr = f"  {'arm':<22} {'recall':>7} {'prec_lb':>8} {'n_sel':>6}"
    for g in GROUPS:
        hdr += f" {g:>22}"
    print(hdr)
    for lab, _, _ in ARMS:
        d = tot[lab]
        if not d["n"]:
            continue
        row = (f"  {lab:<22} {d['rec'] / d['n'] * 100:>6.1f}% "
               f"{d['prec'] / d['pn'] * 100 if d['pn'] else 0:>7.1f}% "
               f"{d['sel'] / d['n']:>6.1f}")
        for g in GROUPS:
            hit, n = d[g]
            row += f" {hit / n * 100 if n else 0:>21.1f}%"
        print(row)
    # same-file reads 0% by construction: scores_for_files excludes the
    # changed files themselves, so co-location is never a history hit.
    ceil = f"  {'-- hist-reach ceiling':<22} {'':>7} {'':>8} {'':>6}"
    for g in GROUPS:
        hit, n = reach[g]
        ceil += f" {hit / n * 100 if n else 0:>21.1f}%"
    print(ceil)
    print("  n per group: " + "  ".join(
        f"{g}={tot[ARMS[0][0]][g][1]}" for g in GROUPS) + "\n")


def precision_sweep(repos, ncases):
    """Can thresholding buy history a usable operating point? (No.)"""
    loaded = [x for x in (load_repo(n, ncases) for n in repos) if x]
    print(f"\n=== co-change signal precision vs reach "
          f"({len(loaded)} repos, <={ncases} cases each) ===")
    print(f"  {'min_cochanges':>13} {'files_lit':>10} {'sig_prec':>9} {'gt_reach':>9}")
    for mc in (2, 3, 5, 8, 12):
        lit, prec, reach = [], [], [0, 0]
        for path, idx, cases, eval_commits in loaded:
            cci = CoChangeIndex(path, exclude_commits=eval_commits,
                                min_cochanges=mc)
            for c in cases:
                hs = cci.scores_for_symbols(c.changed)
                gtf = ({g.split(":")[0] for g in c.must_include}
                       - {x.split(":")[0] for x in c.changed})
                if not gtf:
                    continue
                for f in gtf:
                    reach[1] += 1
                    if hs.get(f, 0.0) > 0.0:
                        reach[0] += 1
                if hs:
                    lit.append(len(hs))
                    prec.append(len(set(hs) & gtf) / len(hs))
        print(f"  {mc:>13} {statistics.mean(lit):>10.1f} "
              f"{statistics.mean(prec) * 100:>8.1f}% "
              f"{reach[0] / reach[1] * 100:>8.1f}%")
    print("\n  Reach falls ~2x faster than precision rises: there is no\n"
          "  threshold at which file-level co-change becomes selective.\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--repos", choices=["frozen", "train", "all"],
                    default="frozen")
    ap.add_argument("--cases", type=int, default=150,
                    help="max mined cases per repo (20/repo inflates effects ~4x)")
    ap.add_argument("--precision", action="store_true",
                    help="run the min_cochanges precision/reach sweep instead")
    args = ap.parse_args()

    repos = {"frozen": FROZEN, "train": TRAIN, "all": TRAIN + FROZEN}[args.repos]
    if args.precision:
        precision_sweep(repos, args.cases)
    else:
        run(repos, f"{args.repos.upper()} REPOS", args.cases)
