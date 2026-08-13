#!/usr/bin/env python3
"""
symbol_cochange_reach.py — feasibility ceiling for SYMBOL-level co-change.

`history_signal_sweep.py` showed file-level co-change reaches 80.3% of
cross-file-other ground truth and still buys no recall, because it lights
20-45 files to hit ~2 relevant ones. The obvious next move is to mine
co-change at function granularity instead: precise by construction, since
a function that never co-changed with the query scores nothing.

The diff->function mapping that would require ALREADY EXISTS and ships --
`diffcontext/verify/history.py` does exactly this to build the benchmark's
ground truth:

    _get_changed_line_ranges   git diff -U0 hunks -> new-file line numbers
    _get_source_at_commit      git show <hash>:<file>  (historical source)
    _find_functions_at_lines   AST-parse THAT source, map lines -> symbol ids

So this script mines symbol-level co-change with those same helpers and the
same noisy-commit thresholds, then measures the one number that decides the
idea before anyone builds a ranker on it:

    reach@k = fraction of ground-truth symbols that co-changed with the
              query symbol >= k times in commits OTHER than the evaluated
              one (the evaluated commit's own contribution is subtracted --
              it IS the label).

Result (requests/starlette/rich, 1500 commits each):

    group                    n    reach>=1  reach>=2   file-level reach
    same-file              734      29.3%     12.1%    0.0 (by construction)
    cross-file neighbour    47       8.5%      2.1%    77.9%
    cross-file other       480      12.1%      1.9%    80.3%
    symbols lit per query: 8.2 (>=1), 1.6 (>=2)        20-45 FILES

The precision intuition is right and it does not matter. Symbol-level
co-change is ~7x sparser than file-level at >=1 and ~40x at >=2. Its ENTIRE
reach ceiling on cross-file-other (12.1%, at a threshold where one shared
commit is usually coincidence) sits BELOW the 14.0% that shipped ranking
already achieves there. A perfect ranker over this signal would lose recall.

Root cause is corpus, not code: requests has only 77 commits in 1500 where
>=2 non-test functions changed together. A matrix like
co_change[authenticate_user][validate_card] = 7 essentially never populates
at function granularity in these repos, and the 29.3% same-file reach says
most of what does populate is re-deriving co-location, which the same-file
weight already captures at ~80% recall.

SECOND, INDEPENDENT REASON TO DISTRUST ANY NUMBER FROM THIS SIGNAL: the
benchmark's ground truth IS symbol-level co-change, mined by these same
helpers from the same commit stream (one commit's changed symbols; one is
the query, the rest are the label). A symbol co-change feature and the
label are therefore the same construct. `exclude_commits` prevents literal
leakage but not that circularity -- such an eval measures the
autocorrelation of the mining function, not retrieval usefulness, and the
number would not transfer to the agent layer.

Usage:
    python benchmarks/symbol_cochange_reach.py
    python benchmarks/symbol_cochange_reach.py --repos black rich --commits 3000
"""

import argparse
import os
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diffcontext.pipeline import index_repository
from diffcontext.verify import cases as vcases
from diffcontext.verify.history import (
    NOISY_FILES, NOISY_SYMBOLS, _find_functions_at_lines,
    _get_changed_line_ranges,
)

BENCH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmark_repos"
)
GROUPS = ("same-file", "cross-file neighbour", "cross-file other")
# What history_signal_sweep.py measured, for side-by-side reading.
FILE_LEVEL_REACH = {
    "same-file": "0.0 (by construction)",
    "cross-file neighbour": "77.9%",
    "cross-file other": "80.3%",
}


def mine_symbol_commits(repo_path, max_commits):
    """[(short_hash, [symbol_id, ...]), ...] via the shipped diff->function map.

    Filters mirror extract_cochange_cases exactly (NOISY_FILES, NOISY_SYMBOLS,
    >=2 symbols, modified-only, no tests) so the mined population is the same
    one the ground truth is drawn from.
    """
    log = subprocess.run(
        ["git", "log", f"--max-count={max_commits}", "--no-merges", "--format=%H"],
        cwd=repo_path, capture_output=True, text=True, timeout=120,
    ).stdout.split()
    out = []
    for h in log:
        files = subprocess.run(
            ["git", "diff", "--name-only", "--relative", "--diff-filter=M",
             f"{h}~1", h],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        ).stdout.strip().split("\n")
        py = [
            f for f in files
            if f.endswith(".py") and f.strip()
            and "/test" not in f.lower() and "/tests/" not in f.lower()
            and "test_" not in os.path.basename(f)
        ]
        if not py or len(py) >= NOISY_FILES:
            continue
        syms = []
        for fp in py:
            lines = _get_changed_line_ranges(repo_path, h, fp)
            if lines:
                syms.extend(_find_functions_at_lines(fp, lines, repo_path, h))
        syms = list(dict.fromkeys(syms))
        if 2 <= len(syms) < NOISY_SYMBOLS:
            out.append((h[:8], syms))
    return out


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


def main(repos, max_commits, ncases):
    print(f"symbol-level co-change reach  (<={max_commits} commits mined/repo)\n")
    pooled = {g: {"n": 0, "r1": 0, "r2": 0} for g in GROUPS}
    lit1, lit2 = [], []

    for name in repos:
        path = os.path.join(BENCH, name)
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        t = time.perf_counter()
        commits = mine_symbol_commits(path, max_commits)
        pairs = defaultdict(Counter)
        for _h, syms in commits:
            for a in syms:
                for b in syms:
                    if a != b:
                        pairs[a][b] += 1
        idx = index_repository(path)
        cases = vcases.cases_from_history(path, max_cases=ncases,
                                          known_symbols=set(idx.symbols))
        by_hash = dict(commits)
        print(f"  [{name}] {len(commits)} usable commits, "
              f"{sum(len(v) for v in pairs.values())} symbol pairs, "
              f"{len(cases)} cases  ({time.perf_counter() - t:.0f}s)", flush=True)

        for c in cases:
            q = c.changed[0]
            own = by_hash.get(c.name.split("-")[1], [])
            # Subtract the evaluated commit's own co-occurrences: that commit
            # is the label, so counting it would score the answer key.
            own_partners = set(own) - {q} if q in own else set()
            eff = {b: n - (1 if b in own_partners else 0)
                   for b, n in pairs.get(q, Counter()).items()}
            eff = {b: n for b, n in eff.items() if n > 0}
            lit1.append(len(eff))
            lit2.append(sum(1 for n in eff.values() if n >= 2))
            for g in c.must_include:
                grp = classify(idx, c.changed, g)
                pooled[grp]["n"] += 1
                pooled[grp]["r1"] += 1 if eff.get(g, 0) >= 1 else 0
                pooled[grp]["r2"] += 1 if eff.get(g, 0) >= 2 else 0

    print(f"\n  {'group':<22} {'n':>6} {'reach>=1':>9} {'reach>=2':>9}"
          f"   {'file-level reach':>18}")
    for g in GROUPS:
        d = pooled[g]
        if not d["n"]:
            continue
        print(f"  {g:<22} {d['n']:>6} {d['r1'] / d['n'] * 100:>8.1f}% "
              f"{d['r2'] / d['n'] * 100:>8.1f}%   {FILE_LEVEL_REACH[g]:>18}")
    if lit1:
        print(f"\n  symbols lit per query: mean {statistics.mean(lit1):.1f} "
              f"(>=1 co-change), {statistics.mean(lit2):.1f} (>=2)"
              f"   [file-level lit 20-45 FILES]")
    print("\n  Precise, and too sparse to matter: cross-file-other reach is\n"
          "  below the recall shipped ranking already gets on that group.\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--repos", nargs="+",
                    default=["requests", "starlette", "rich"])
    ap.add_argument("--commits", type=int, default=1500,
                    help="git log depth to mine per repo")
    ap.add_argument("--cases", type=int, default=150)
    args = ap.parse_args()
    main(args.repos, args.commits, args.cases)
