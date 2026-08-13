"""
measure_cap_exemption.py — pick selector.CAP_EXEMPT_TOP_N from data.

Question
--------
selector.py caps any single symbol at MAX_SINGLE_SYMBOL_FRACTION of the token
budget and skips it entirely if it exceeds that. The cap was score-blind, so
it could evict the top-ranked candidate for the crime of being long. Large
functions are rare (0.0-1.2% of symbols across these repos) but they are
disproportionately the hubs — the orchestrator everything calls — so the
eviction lands on exactly the symbols retrieval exists to find.

CAP_EXEMPT_TOP_N exempts the N highest-ranked candidates from the cap. They
remain subject to the token budget. This script sweeps N.

Why the cap can be relaxed safely: the budget loop uses `continue`, not
`break`, so an oversized symbol that does not fit is skipped and smaller
candidates are still considered. The cap is a second, redundant exclusion
that fires even when the budget is uncontended.

Method
------
Ground truth is git co-change (verify/history.py), the same source the
published benchmark uses, with the same mechanical-refactor exclusions.
Recall is measured against those co-changed symbols; precision is a LOWER
BOUND because co-change ground truth is incomplete.

Run:
    python -m benchmarks.measure_cap_exemption
    python -m benchmarks.measure_cap_exemption --cases 30
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


def run_repo(path, n_cases):
    idx = index_repository(path)
    known = set(idx.symbols)
    cases = vcases.cases_from_history(path, max_cases=n_cases)
    # A case whose QUERY symbol is not in the index compiles nothing, so its
    # recall is 0 regardless of policy. Keeping those would damp every arm
    # equally and understate the differences between them.
    cases = [c for c in cases if all(s in known for s in c.changed)]
    if len(cases) < 5:
        return None

    out = {}
    for n in SWEEP:
        selector.CAP_EXEMPT_TOP_N = n
        res = vcases.run_cases(path, cases, index=idx)
        k = len(res)
        pl = [r.precision_lb for r in res if r.precision_lb is not None]
        out[n] = {
            "n": k,
            "pass": sum(1 for r in res if r.passed),
            "recall": sum(r.recall for r in res) / k,
            "prec": (sum(pl) / len(pl)) if pl else 0.0,
            "tokens": sum(r.context_tokens for r in res) / k,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=20)
    ap.add_argument("--repos", nargs="*", default=None)
    ap.add_argument("--extra-repo", action="append", default=[],
                    help="absolute path to an additional repo to include")
    args = ap.parse_args()

    targets = [(r, os.path.join(BENCH, r)) for r in (args.repos or DEFAULT_REPOS)]
    targets += [(os.path.basename(p.rstrip("/")), p) for p in args.extra_repo]

    per_repo, pooled = {}, {n: [0, 0.0, 0.0, 0.0, 0] for n in SWEEP}
    for name, path in targets:
        if not os.path.isdir(os.path.join(path, ".git")):
            print(f"  skip {name}: not a git repo", file=sys.stderr)
            continue
        r = run_repo(path, args.cases)
        if r is None:
            print(f"  skip {name}: too few usable cases", file=sys.stderr)
            continue
        per_repo[name] = r
        for n in SWEEP:
            d = r[n]
            pooled[n][0] += d["pass"]
            pooled[n][1] += d["recall"] * d["n"]
            pooled[n][2] += d["prec"] * d["n"]
            pooled[n][3] += d["tokens"] * d["n"]
            pooled[n][4] += d["n"]

    def label(n):
        return "cap always on" if n == 0 else ("cap off" if n > 1000 else f"top-{n} exempt")

    print(f"\n{'repo':<14} " + " ".join(f"{label(n):>16}" for n in SWEEP))
    for name, r in per_repo.items():
        cells = " ".join(f"{r[n]['recall']*100:>7.1f}% {r[n]['pass']:>3}/{r[n]['n']:<3}"
                         for n in SWEEP)
        print(f"{name:<14} {cells}")

    tot = pooled[SWEEP[0]][4]
    if not tot:
        print("\nno repos measured")
        return
    print(f"\nPOOLED over {len(per_repo)} repos / {tot} cases")
    print(f"  {'policy':<16} {'pass':>9} {'recall':>8} {'prec_lb':>8} {'tok/case':>9}")
    for n in SWEEP:
        p, rec, pr, tk, k = pooled[n]
        print(f"  {label(n):<16} {p:>4}/{k:<4} {rec/k*100:>7.1f}% "
              f"{pr/k*100:>7.1f}% {tk/k:>9,.0f}")


if __name__ == "__main__":
    main()
