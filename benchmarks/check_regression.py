#!/usr/bin/env python3
"""
check_regression.py — retrieval-quality gate for CI and pre-release checks.

Re-runs the hardened benchmark (eval_v2) on one repo and FAILS LOUDLY if
retrieval quality drops below frozen floors. Run it before every release:

    python benchmarks/check_regression.py                  # flask (fast, ~1 min)
    python benchmarks/check_regression.py benchmark_repos/django

Floors are set ~0.09-0.12 below the values measured on 2026-07-20, when
they were re-frozen after HYBRID_WEIGHTS moved to the LORO-validated
[0.3, 0.5, 0.2] (measured then: flask diffcontext 0.773/0.579, hybrid
0.863/0.694; django diffcontext 0.795/0.664, hybrid 0.897/0.787 — every
floor tightened, none loosened). Ordinary sampling noise passes but a
real regression trips the gate. If you deliberately change retrieval
behavior, re-run the benchmark on both gate repos, record the numbers
here, and only then adjust these floors — never loosen them to make a
red gate green.

Exit code: 0 = pass, 1 = regression detected.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.eval_v2_hardened import evaluate_repo

# floors: repo -> {method: {metric: floor}}   (per-commit aggregates)
#
# Only the product's own methods (diffcontext, hybrid) have floors. Baselines
# (bm25, embedding, samefile, random_k) are comparison points, not guarded
# behavior — and the embedding baseline's numbers depend on which encoder is
# installed in the environment (sentence-transformers vs the TF-IDF fallback),
# so freezing a floor for it would make the gate flaky by construction.
FLOORS = {
    "flask": {
        "diffcontext": {"hit": 0.68, "recall": 0.48},
        "hybrid":      {"hit": 0.77, "recall": 0.58},
    },
    "django": {
        "diffcontext": {"hit": 0.70, "recall": 0.56},
        "hybrid":      {"hit": 0.81, "recall": 0.69},
    },
}


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "..", "benchmark_repos", "flask")
    repo = os.path.normpath(repo)
    name = os.path.basename(repo)
    if name not in FLOORS:
        print(f"No floors defined for '{name}' — add them to FLOORS first.")
        sys.exit(1)

    result = evaluate_repo(repo)
    if result is None:
        print("REGRESSION GATE ERROR: benchmark produced no result.")
        sys.exit(1)

    failures = []
    for method, floors in FLOORS[name].items():
        per_commit = result["summary"]["methods"][method]["per_commit"]
        for metric, floor in floors.items():
            actual = per_commit[metric]
            status = "PASS" if actual >= floor else "FAIL"
            print(f"  {status}  {name}/{method}/{metric}: {actual:.3f} (floor {floor})")
            if actual < floor:
                failures.append(f"{method}/{metric}={actual:.3f} < {floor}")

    if failures:
        print(f"\nREGRESSION DETECTED on {name}: " + "; ".join(failures))
        sys.exit(1)
    print(f"\nGate passed on {name}.")


if __name__ == "__main__":
    main()
