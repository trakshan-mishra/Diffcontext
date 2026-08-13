"""
measure_cap_exemption.py — pick selector.CAP_EXEMPT_TOP_N from data.

selector.py caps any single symbol at MAX_SINGLE_SYMBOL_FRACTION of the token
budget and skips it entirely if it exceeds that. The cap was score-blind, so it
could evict the top-ranked candidate for the crime of being long. Large
functions are rare but they are disproportionately the hubs, so the eviction
lands on exactly the symbols retrieval exists to find. CAP_EXEMPT_TOP_N exempts
the N highest-ranked candidates; they remain subject to the token budget. This
sweeps N, and (with --budget) sweeps the budget too, since a cap can only bind
under budget pressure.

Ground truth is git co-change (verify/history.py), the same source and the same
mechanical-refactor exclusions the published benchmark uses, filtered to symbols
that still exist at HEAD. Precision is a LOWER BOUND: co-change ground truth is
incomplete.

    python -m benchmarks.measure_cap_exemption
    python -m benchmarks.measure_cap_exemption --budget 1500 3000 6000 10000
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.pipeline import index_repository
from diffcontext.verify import cases as vcases
from diffcontext.context import selector

BENCH = os.path.join(os.path.dirname(__file__), "..", "benchmark_repos")
DEFAULT_REPOS = [
    "requests", "flask", "click", "rich", "httpx",
    "starlette", "black", "pydantic", "django",
]
SWEEP = (0, 5, 10, 20, 10**9)   # 0 = cap always on (old behaviour); huge = cap off


def run_repo(path, n_cases, budget):
    """Sweep CAP_EXEMPT_TOP_N on one repo at one budget.

    Returns raw SUMS, not per-repo means: pooling means across repos would
    weight a 6-case repo like a 40-case one, and precision has its own
    denominator (cases that retrieved nothing beyond the changed symbols have
    no defined precision and must not be imputed the mean).
    """
    idx = index_repository(path)
    # Mined ground truth is scored against HEAD, so symbols deleted upstream
    # are unretrievable by construction; counting them measures repo churn.
    # verify passes the same filter, so these numbers stay comparable to it.
    cases = vcases.cases_from_history(
        path, max_cases=n_cases, known_symbols=set(idx.symbols),
    )
    if len(cases) < 5:
        return None
    for c in cases:
        c.budget = budget

    out = {}
    original = selector.CAP_EXEMPT_TOP_N
    try:
        for n in SWEEP:
            selector.CAP_EXEMPT_TOP_N = n
            res = vcases.run_cases(path, cases, index=idx)
            pl = [r.precision_lb for r in res if r.precision_lb is not None]
            out[n] = {
                "n": len(res),
                "pass": sum(1 for r in res if r.passed),
                "recall_sum": sum(r.recall for r in res),
                "prec_sum": sum(pl),
                "prec_n": len(pl),
                "tok_sum": sum(r.context_tokens for r in res),
            }
    finally:
        # Restore: this module-level knob is process-global and later code
        # (or another repo in this same run) must not inherit the last sweep.
        selector.CAP_EXEMPT_TOP_N = original
    return out


def label(n):
    return "cap always on" if n == 0 else ("cap off" if n > 1000 else f"top-{n} exempt")


def measure(targets, n_cases, budget):
    per_repo = {}
    pooled = {n: {"pass": 0, "recall_sum": 0.0, "prec_sum": 0.0,
                  "prec_n": 0, "tok_sum": 0.0, "n": 0} for n in SWEEP}
    for name, path in targets:
        if not os.path.isdir(os.path.join(path, ".git")):
            print(f"  skip {name}: not a git repo", file=sys.stderr)
            continue
        r = run_repo(path, n_cases, budget)
        if r is None:
            print(f"  skip {name}: too few usable cases", file=sys.stderr)
            continue
        per_repo[name] = r
        for n in SWEEP:
            for k in pooled[n]:
                pooled[n][k] += r[n][k]
    return per_repo, pooled


def main():
    ap = argparse.ArgumentParser()
    # 20/repo (the old default) is underpowered enough to flip this effect's
    # sign at tight budgets. 150 is ~1,100 cases and ~5 min for two budgets.
    ap.add_argument("--cases", type=int, default=150)
    ap.add_argument("--repos", nargs="*", default=None)
    ap.add_argument("--budget", nargs="*", type=int, default=[vcases.DEFAULT_BUDGET],
                    help="token budgets to sweep; a cap only binds under pressure")
    ap.add_argument("--extra-repo", action="append", default=[],
                    help="absolute path to an additional repo to include")
    args = ap.parse_args()

    targets = [(r, os.path.join(BENCH, r)) for r in (args.repos or DEFAULT_REPOS)]
    targets += [(os.path.basename(p.rstrip("/")), p) for p in args.extra_repo]

    for budget in args.budget:
        per_repo, pooled = measure(targets, args.cases, budget)
        tot = pooled[SWEEP[0]]["n"]
        if not tot:
            print(f"\nbudget {budget:,}: no repos measured")
            continue

        print(f"\n=== budget {budget:,} tokens ===")
        print(f"{'repo':<14} " + " ".join(f"{label(n):>16}" for n in SWEEP))
        for name, r in per_repo.items():
            cells = " ".join(
                f"{r[n]['recall_sum'] / r[n]['n'] * 100:>7.1f}% {r[n]['pass']:>3}/{r[n]['n']:<3}"
                for n in SWEEP
            )
            print(f"{name:<14} {cells}")

        print(f"\nPOOLED over {len(per_repo)} repos / {tot} cases")
        print(f"  {'policy':<16} {'pass':>9} {'recall':>8} {'prec_lb':>8} {'tok/case':>9}")
        for n in SWEEP:
            d = pooled[n]
            # prec has its own denominator: only cases that retrieved something.
            prec = (d["prec_sum"] / d["prec_n"] * 100) if d["prec_n"] else float("nan")
            print(f"  {label(n):<16} {d['pass']:>4}/{d['n']:<4} "
                  f"{d['recall_sum'] / d['n'] * 100:>7.1f}% {prec:>7.1f}% "
                  f"{d['tok_sum'] / d['n']:>9,.0f}")


if __name__ == "__main__":
    main()
