#!/usr/bin/env python3
"""
ground_truth.py — co-change ground truth extraction (re-export shim).

The extractor now lives in the installable package at
diffcontext/verify/history.py so `diffcontext verify --from-history`
works outside this repo. This module keeps the old import path alive
for the benchmark scripts (eval_v1, eval_v2_hardened, benchmark_runner,
diagnose_graph_gaps) — including the private helpers eval_v2 uses.

See diffcontext/verify/history.py for the methodology and the bug-fix
history (git-show-at-commit, per-symbol case expansion, etc.).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.verify.history import (  # noqa: F401
    CoChangeCase,
    extract_cochange_cases,
    _get_changed_line_ranges,
    _get_source_at_commit,
    _find_parent_class,
    _find_functions_at_lines,
)

if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    cases = extract_cochange_cases(repo, max_cases=50)
    print(f"Found {len(cases)} co-change test cases")
    for case in cases[:5]:
        print(f"\n  Commit: {case.commit_hash} — {case.commit_msg}")
        print(f"  Query:  {case.query_symbol}")
        print(f"  Ground truth ({len(case.ground_truth_symbols)}):")
        for gt in case.ground_truth_symbols[:5]:
            print(f"    {gt}")
