#!/usr/bin/env python3
"""
tasks.py — mine test-verifiable fix tasks from a repo's own history.

SWE-bench-style construction, applied to the benchmark repos:

  A commit C that changes BOTH production functions AND test files defines
  a candidate task. The task state is `C~1` (parent code) with C's test
  files checked out on top. A candidate becomes a task only if BOTH
  machine-checked properties hold:

    fail@state : the changed test files FAIL at the task state
    pass@gold  : the same test files PASS at C (equivalently: applying the
                 gold code patch to the task state makes them pass)

  Every emitted task therefore has an executable, repo-native judge — no
  LLM grading, no proxy metric. This is the property co-change recall
  cannot give us and the whole point of rung 5.

Validation is expensive (two pytest runs per candidate), so mining stops
at --target tasks. Worktrees are used so the benchmark repo clone is
never mutated.

Usage:
  python benchmarks/downstream/tasks.py benchmark_repos/click --target 20
  # writes benchmarks/downstream/tasks/click.json
"""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from benchmarks.ground_truth import _get_changed_line_ranges, _find_functions_at_lines

TASKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks")
SCAN_LIMIT = 400          # commits of history to scan for candidates
TEST_TIMEOUT = 240        # seconds per pytest run
MAX_TEST_FILES = 4        # candidates touching more test files are refactors


@dataclass
class Task:
    repo: str
    commit: str               # the gold commit (full sha)
    parent: str               # task-state base (full sha)
    commit_msg: str
    test_files: List[str]     # C's test files, checked out onto the parent
    code_files: List[str]     # production files the gold patch touches
    gold_patch: str           # git diff parent..commit -- code_files
    seed_symbols: List[str]   # gold-changed function IDs that EXIST at parent
    added_symbols: List[str] = field(default_factory=list)  # changed at C but absent at parent
    fail_output: str = ""     # tail of pytest output at the task state


def _git(repo: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=timeout)


def _run_tests(worktree: str, test_files: List[str]) -> subprocess.CompletedProcess:
    """Run pytest on the given test files inside the worktree.

    src-layout repos need the package importable without installation;
    PYTHONPATH covers both src/ and flat layouts.
    """
    env = dict(os.environ)
    pypath = [os.path.join(worktree, "src"), worktree]
    env["PYTHONPATH"] = os.pathsep.join(pypath)
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", "--no-header",
         "-p", "no:cacheprovider", *test_files],
        cwd=worktree, capture_output=True, text=True,
        timeout=TEST_TIMEOUT, env=env,
    )


class Worktree:
    """A detached git worktree that can be moved between states."""

    def __init__(self, repo: str, path: str, sha: str):
        self.repo, self.path = os.path.abspath(repo), path
        if os.path.exists(path):
            self.remove()
        r = _git(self.repo, "worktree", "add", "--detach", path, sha)
        if r.returncode != 0:
            raise RuntimeError(f"worktree add failed: {r.stderr.strip()}")

    def checkout(self, sha: str) -> None:
        for cmd in (("checkout", "-f", "--detach", sha), ("clean", "-fdq")):
            r = _git(self.path, *cmd)
            if r.returncode != 0:
                raise RuntimeError(f"git {cmd[0]} failed: {r.stderr.strip()}")

    def overlay_files(self, sha: str, files: List[str]) -> None:
        """Check out specific files from `sha` on top of the current state."""
        r = _git(self.path, "checkout", sha, "--", *files)
        if r.returncode != 0:
            raise RuntimeError(f"overlay failed: {r.stderr.strip()}")

    def remove(self) -> None:
        _git(self.repo, "worktree", "remove", "--force", self.path)
        shutil.rmtree(self.path, ignore_errors=True)
        _git(self.repo, "worktree", "prune")


def _is_test_file(path: str) -> bool:
    base = os.path.basename(path)
    return path.endswith(".py") and (
        base.startswith("test_") or base.endswith("_test.py") or "/tests/" in path
    )


def find_candidates(repo: str, scan_limit: int = SCAN_LIMIT) -> List[dict]:
    """Commits (newest first) that modify both production .py and test files."""
    log = _git(repo, "log", f"--max-count={scan_limit}", "--format=%H|%s",
               "--no-merges", "--diff-filter=M")
    if log.returncode != 0:
        return []
    out = []
    for line in log.stdout.strip().splitlines():
        if "|" not in line:
            continue
        sha, msg = line.split("|", 1)
        files_r = _git(repo, "diff", "--name-only", "--diff-filter=M", f"{sha}~1", sha)
        if files_r.returncode != 0:
            continue
        files = [f for f in files_r.stdout.strip().splitlines() if f.strip()]
        tests = [f for f in files if _is_test_file(f)]
        code = [f for f in files if f.endswith(".py") and not _is_test_file(f)]
        if code and 0 < len(tests) <= MAX_TEST_FILES:
            out.append({"sha": sha, "msg": msg, "tests": tests, "code": code})
    return out


def validate_candidate(repo: str, cand: dict, scratch: str) -> Optional[Task]:
    """Build the task state and machine-check fail@state / pass@gold."""
    repo = os.path.abspath(repo)
    sha, parent = cand["sha"], cand["sha"] + "~1"
    parent_sha = _git(repo, "rev-parse", parent).stdout.strip()
    wt_path = os.path.join(scratch, "wt")

    try:
        wt = Worktree(repo, wt_path, parent_sha)
    except RuntimeError:
        return None
    try:
        # pass@gold: the full commit must be green on its own tests.
        wt.checkout(sha)
        try:
            gold = _run_tests(wt.path, cand["tests"])
        except subprocess.TimeoutExpired:
            return None
        if gold.returncode != 0:
            return None  # flaky or env-dependent tests: unusable as a judge

        # fail@state: parent code + C's tests must be red.
        wt.checkout(parent_sha)
        try:
            wt.overlay_files(sha, cand["tests"])
        except RuntimeError:
            return None  # test file didn't exist at parent in a checkoutable way
        try:
            state = _run_tests(wt.path, cand["tests"])
        except subprocess.TimeoutExpired:
            return None
        if state.returncode == 0:
            return None  # tests pass without the fix: no signal

        gold_patch = _git(repo, "diff", parent_sha, sha, "--", *cand["code"]).stdout
        if not gold_patch.strip():
            return None

        # Gold-changed function IDs (the oracle-localization seeds).
        changed: List[str] = []
        for fp in cand["code"]:
            lines = _get_changed_line_ranges(repo, sha, fp)
            if lines:
                changed.extend(_find_functions_at_lines(fp, lines, repo, sha))
        changed = list(dict.fromkeys(changed))
        if not changed:
            return None

        # Which of them exist at the parent (a seed must be retrievable
        # BEFORE the fix)? Functions the commit introduces are recorded
        # separately — providers can't be seeded with them.
        parent_files = _git(repo, "ls-tree", "-r", "--name-only", parent_sha).stdout
        present = set(parent_files.splitlines())
        seeds, added = [], []
        for sym in changed:
            path = sym.split(":", 1)[0].lstrip("./")
            (seeds if path in present else added).append(sym)
        if not seeds:
            return None

        tail = (state.stdout + state.stderr)[-3000:]
        return Task(
            repo=os.path.basename(repo), commit=_git(repo, "rev-parse", sha).stdout.strip(),
            parent=parent_sha, commit_msg=cand["msg"][:200],
            test_files=cand["tests"], code_files=cand["code"],
            gold_patch=gold_patch, seed_symbols=seeds,
            added_symbols=added, fail_output=tail,
        )
    finally:
        wt.remove()


def mine(repo: str, target: int, scan_limit: int, scratch: str) -> List[Task]:
    tasks: List[Task] = []
    cands = find_candidates(repo, scan_limit)
    print(f"{len(cands)} candidate commits (code+tests both modified)")
    for i, cand in enumerate(cands):
        if len(tasks) >= target:
            break
        t = validate_candidate(repo, cand, scratch)
        status = "TASK" if t else "skip"
        print(f"  [{i + 1}/{len(cands)}] {cand['sha'][:10]} {status}  {cand['msg'][:60]}")
        if t:
            tasks.append(t)
    return tasks


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo", help="path to a benchmark repo clone")
    ap.add_argument("--target", type=int, default=20)
    ap.add_argument("--scan-limit", type=int, default=SCAN_LIMIT)
    ap.add_argument("--scratch", default=os.environ.get("TMPDIR", "/tmp"))
    args = ap.parse_args()

    scratch = os.path.join(args.scratch, "diffcontext-downstream-mine")
    os.makedirs(scratch, exist_ok=True)
    tasks = mine(args.repo, args.target, args.scan_limit, scratch)

    os.makedirs(TASKS_DIR, exist_ok=True)
    out = os.path.join(TASKS_DIR, os.path.basename(os.path.abspath(args.repo)) + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "tasks": [asdict(t) for t in tasks]}, f, indent=1)
    print(f"\n{len(tasks)} validated task(s) -> {out}")


if __name__ == "__main__":
    main()
