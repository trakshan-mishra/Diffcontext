#!/usr/bin/env python3
"""
run_official_eval.py — run the official ContextBench evaluator
(`python -m contextbench.evaluate`) on the retrieval predictions, WITHOUT
touching the canonical clones.

WHY THIS EXISTS: the evaluator's `contextbench.core.repo.checkout` runs
`git fetch --depth 1 --filter=blob:none origin <commit>` on whatever
`base_dir` it is pointed at (repo.py:75-80). If that base_dir is one of our
canonical full clones, the fetch writes a `.git/shallow` file and converts it
to a shallow repo, which then breaks `ScratchClone`'s `git clone --local` +
`checkout` in run_glm_pass1.py ("fatal: unable to read tree"). This script
makes DISPOSABLE local copies of the canonical clones, rewrites the pred
files' `repo_url` to point at the copies, runs the evaluator against the
copies, and removes them after. The canonical clones are never mutated.

Usage:
  python3 benchmarks/contextbench/run_official_eval.py \
      --pred-dir benchmarks/contextbench/results/retrieval_136_4var \
      --out-dir  benchmarks/contextbench/results/official_eval_136
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Dict

HERE = os.path.dirname(os.path.abspath(__file__))

CANONICAL_CLONES = {
    "django": os.environ.get("DJANGO_CLONE_PATH",
                             "/home/trakshan/cb/django_canonical"),
    "requests": os.environ.get("REQUESTS_CLONE_PATH",
                                "/home/trakshan/cb/requests_canonical"),
    "flask": os.environ.get("FLASK_CLONE_PATH",
                            "/home/trakshan/cb/flask_canonical"),
}
# ContextBench `repo` field -> local clone key.
REPO_TO_KEY = {"django/django": "django", "psf/requests": "requests",
               "pallets/flask": "flask"}

# The evaluator package (cloned from github.com/EuniAI/ContextBench).
EVAL_REPO_DEFAULT = "/tmp/opencode/ContextBench"
GOLD_PARQUET = ("/home/trakshan/.cache/huggingface/hub/"
                "datasets--Contextbench--ContextBench/snapshots/"
                "c2855792b006af41c67202d33883fb9d46362853/data/full.parquet")


def make_disposable_clones(work_root: str) -> Dict[str, str]:
    """`git clone --local` each canonical clone into work_root. Fast (hardlinks
    share objects); writes to the copy's own .git never touch the canonical."""
    copies = {}
    for key, canonical in CANONICAL_CLONES.items():
        if not os.path.isdir(os.path.join(canonical, ".git")):
            print(f"[skip] no canonical clone for {key} at {canonical}",
                  file=sys.stderr)
            continue
        dest = os.path.join(work_root, key)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        r = subprocess.run(["git", "clone", "--local", canonical, dest],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            sys.exit(f"disposable clone failed for {key}: {r.stderr.strip()}")
        copies[key] = dest
        print(f"[clone] {key} -> {dest}", file=sys.stderr)
    return copies


def rewrite_pred(in_path: str, out_path: str, copies: Dict[str, str]) -> int:
    """Copy a pred JSONL, rewriting `repo_url` to the disposable clone for the
    row's `repo`. Returns the number of rows written."""
    n = 0
    with open(in_path) as fin, open(out_path, "w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            key = REPO_TO_KEY.get(d.get("repo", ""))
            if key and key in copies:
                d["repo_url"] = copies[key]
            fout.write(json.dumps(d) + "\n")
            n += 1
    return n


def run_evaluator(pred_path: str, out_path: str, cache_dir: str,
                  tmp_root: str) -> int:
    """Run `python -m contextbench.evaluate` from the evaluator repo.

    CONTEXTBENCH_TMP_ROOT is set to `tmp_root` (on the main filesystem, NOT
    /tmp) so the evaluator's per-commit worktrees don't exhaust the 5.4G
    tmpfs — django worktrees are ~30MB each × 136 commits = 4G+."""
    cmd = [sys.executable, "-m", "contextbench.evaluate",
           "--gold", GOLD_PARQUET, "--pred", pred_path,
           "--cache", cache_dir, "--out", out_path]
    if not os.path.isdir(EVAL_REPO_DEFAULT):
        sys.exit(f"evaluator repo not found at {EVAL_REPO_DEFAULT} "
                 "(clone github.com/EuniAI/ContextBench there)")
    env = dict(os.environ)
    env["CONTEXTBENCH_TMP_ROOT"] = tmp_root
    r = subprocess.run(cmd, cwd=EVAL_REPO_DEFAULT, text=True,
                       capture_output=False, timeout=1800, env=env)
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred-dir",
                   default=os.path.join(HERE, "results", "retrieval_136_4var"))
    ap.add_argument("--out-dir",
                   default=os.path.join(HERE, "results", "official_eval_136"))
    ap.add_argument("--variants", default="seeds_only,retrieved_only,"
                                          "seeds_plus_retrieved,diffcontext_gap")
    ap.add_argument("--keep-copies", action="store_true",
                   help="keep the disposable clones after (for debugging)")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    if not os.path.isfile(GOLD_PARQUET):
        sys.exit(f"gold parquet missing: {GOLD_PARQUET}")

    # Put the work root on the SAME filesystem as the canonical clones so
    # `git clone --local` hardlinks succeed (cross-device hardlinks fail with
    # "Invalid cross-device link"). /home/trakshan/cb holds the canonical clones.
    work_root = tempfile.mkdtemp(prefix="cb_eval_clones_",
                                dir="/home/trakshan/cb")
    try:
        copies = make_disposable_clones(work_root)
        cache_dir = os.path.join(work_root, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        # Worktrees go here (main filesystem, NOT /tmp tmpfs).
        tmp_root = os.path.join(work_root, "wt")
        os.makedirs(tmp_root, exist_ok=True)
        for v in args.variants.split(","):
            pred_in = os.path.join(args.pred_dir, f"pred_{v}.jsonl")
            if not os.path.isfile(pred_in):
                print(f"[skip] no pred file: {pred_in}", file=sys.stderr)
                continue
            pred_rw = os.path.join(work_root, f"pred_{v}.jsonl")
            n = rewrite_pred(pred_in, pred_rw, copies)
            out_path = os.path.join(args.out_dir, f"eval_{v}.jsonl")
            print(f"\n=== evaluating {v} ({n} rows) ===", file=sys.stderr)
            rc = run_evaluator(pred_rw, out_path, cache_dir, tmp_root)
            if rc != 0:
                print(f"[warn] evaluator returned {rc} for {v}",
                      file=sys.stderr)
        print(f"\nDone. Results in {args.out_dir}", file=sys.stderr)
    finally:
        if args.keep_copies:
            print(f"disposable clones kept at {work_root}", file=sys.stderr)
        else:
            shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    main()
