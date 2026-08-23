#!/usr/bin/env python3
"""
analyze_arms.py — full 4-arm pass@1 + pairwise McNemar matrix for Task A.

Loads glm_pass1_arms.jsonl, computes pass@1 with Wilson CIs for all four arms
(none, diffcontext, bm25, samefile) and ALL 6 pairwise exact McNemar tests,
so the falsification question (does diffcontext beat bm25 / samefile
downstream?) is answered with the full matrix, not just one comparison.

This is a falsification test: report plainly, do not tune anything to win.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # stats.py is in this directory
from stats import wilson_interval, mcnemar_exact, paired_table  # noqa: E402

ARMS = ("none", "diffcontext", "bm25", "samefile")
NON_EVALUABLE = {"setup_error", "no_seeds", "skipped_no_llm"}


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def main(path):
    rows = load(path)
    print(f"rows: {len(rows)}")
    results = {a: {} for a in ARMS}
    print(f"\n{'arm':<14} {'pass':>5} {'att':>5} {'pass@1':>7} {'95% Wilson CI':>18}  errors")
    print("-" * 78)
    for a in ARMS:
        ar = [r for r in rows if r.get("variant") == a]
        passed = sum(1 for r in ar if r.get("passed") is True)
        attempted = sum(1 for r in ar if r.get("error_class") not in NON_EVALUABLE)
        ne = {e: sum(1 for r in ar if r.get("error_class") == e)
              for e in ("no_seeds", "setup_error", "gen_error", "apply_error", "test_error")}
        lo, hi = wilson_interval(passed, attempted) if attempted else (float("nan"), float("nan"))
        ci = f"[{lo:.3f}, {hi:.3f}]" if lo == lo else "—"
        err = " ".join(f"{k}={v}" for k, v in ne.items() if v)
        print(f"  {a:<12} {passed:>5} {attempted:>5} {passed/attempted*100 if attempted else 0:>6.1f}% {ci:>18}  {err}")
        results[a] = {r["instance_id"]: r.get("passed") is True
                     for r in ar if r.get("error_class") not in NON_EVALUABLE}

    print("\npairwise exact McNemar (attempted tasks, matched pairs):")
    print(f"  {'pair':<32} {'both':>5} {'Aonly':>6} {'Bonly':>6} {'neither':>7} {'p':>10}")
    print("  " + "-" * 72)
    for i, a in enumerate(ARMS):
        for b in ARMS[i + 1:]:
            both, ao, bo, neither = paired_table(results[a], results[b])
            p = mcnemar_exact(ao, bo)
            sig = " ***" if p < 0.001 else " **" if p < 0.01 else " *" if p < 0.05 else ""
            print(f"  {a+' vs '+b:<32} {both:>5} {ao:>6} {bo:>6} {neither:>7} {p:>10.4g}{sig}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "results", "glm_pass1", "glm_pass1_arms.jsonl")
    main(path)
