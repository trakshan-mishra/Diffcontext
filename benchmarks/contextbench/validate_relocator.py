#!/usr/bin/env python3
"""
validate_relocator.py — offline validation of diff_relocator on REAL GLM 5.2
apply-error diffs. No LLM, no API cost.

RESULT: 0/18 recovered across two independent captured samples (12 systematic
+ 6 sporadic). The relocator is a documented negative result — see
diff_relocator.py docstring. This script is retained so the 0/18 can be
regenerated from the captured diffs at any time.

For each captured apply-error (instance, variant):
  1. clone at base_commit, apply test_patch (clean base)
  2. baseline: git apply -p1 on the raw captured diff (confirms it fails)
  3. fix: relocate_hunks() then git apply on the relocated diff
  4. recovered: did the relocator make it apply when baseline failed?
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))

from benchmarks.contextbench.run_glm_pass1 import ScratchClone, apply_patch
from benchmarks.contextbench.diff_relocator import relocate_hunks

SCRATCH_ROOT = "/tmp/opencode/relocator/scratch"
CANONICAL = {"django": os.environ.get("DJANGO_CLONE_PATH",
                                       "/home/trakshan/cb/django_canonical")}
CAPTURED = ["/tmp/opencode/relocator/captured_apply_errors.json",
            "/tmp/opencode/relocator/captured_sporadic.json"]


def try_apply(workdir, diff):
    import subprocess
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(["git", "apply", "-p1"], input=diff, cwd=workdir,
                       capture_output=True, text=True, timeout=30, env=env)
    return r.returncode == 0, (r.stderr or "").strip()[:200]


def main():
    cases = []
    for p in CAPTURED:
        if os.path.isfile(p):
            cases.extend(json.load(open(p)))
    print(f"Validating relocator on {len(cases)} captured apply-error diffs "
          f"(systematic + sporadic)\n")
    os.makedirs(SCRATCH_ROOT, exist_ok=True)
    rec = 0
    for i, c in enumerate(cases, 1):
        iid = c["instance_id"]
        diff = c["diff"]
        scratch = ScratchClone(CANONICAL["django"],
                               os.path.join(SCRATCH_ROOT, f"c{i}"),
                               c["base_commit"])
        try:
            ok_tp, err_tp, _m = apply_patch(scratch.path, c["test_patch"])
            if not ok_tp:
                print(f"[{i:>2}] {iid[-14:]} {c['variant']:<16} SKIP test_patch fail")
                continue
            scratch.reset_to(c["base_commit"])
            apply_patch(scratch.path, c["test_patch"])
            base_ok, base_err = try_apply(scratch.path, diff)
            relocated = relocate_hunks(diff, scratch.path)
            if relocated is None:
                fix_ok, fix_err = False, "relocator None (no context match)"
            else:
                scratch.reset_to(c["base_commit"])
                apply_patch(scratch.path, c["test_patch"])
                fix_ok, fix_err = try_apply(scratch.path, relocated)
            if fix_ok and not base_ok:
                rec += 1
            print(f"[{i:>2}] {iid[-14:]} {c['variant']:<16} "
                  f"baseline={'OK' if base_ok else 'FAIL'}  "
                  f"relocator={'OK' if fix_ok else 'FAIL'}  "
                  f"{'RECOVERED' if (fix_ok and not base_ok) else ''}")
        finally:
            scratch.remove()
    print(f"\n=== RELOCATOR recovered {rec}/{len(cases)} captured apply errors "
          f"({rec/len(cases):.0%}) — offline, no LLM cost ===")


if __name__ == "__main__":
    main()
