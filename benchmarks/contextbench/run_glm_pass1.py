#!/usr/bin/env python3
"""
run_glm_pass1.py — GLM 5.2 pass@1 pilot on ContextBench Python tasks.

For each task: checkout base_commit in a local clone, apply test_patch,
compile DiffContext context (oracle seeds + retrieved supporters), ask
GLM 5.2 for a patch, apply it, run the f2p tests, report pass/fail.

Context variants compared under the same model + prompt:
  none               — just the problem statement (floor / memorization probe)
  diffcontext        — seeds + retrieved (default recall-first retrieval)
  diffcontext_gap    — seeds + gap-cutoff retrieved (the precision improvement)

Arms comparison (--shared-renderer, Task A): diffcontext / bm25 / samefile
all routed through ONE shared renderer (seeds + render_context) so the only
difference is supporter ranking. bm25 and samefile are only meaningful with
--shared-renderer; without it they fall back to empty context.

Oracle localization: seeds are extracted from the gold patch (the functions
the gold fix modifies). This isolates context quality as the variable —
disclosed as a limitation (same methodology as DiffContext's own downstream eval).

Infrastructure: uses a per-task local clone (NOT worktrees) from a canonical
full clone. This avoids the worktree-registry corruption and blobless-clone
hangs that destroyed the earlier runs.

Usage:
  set -a; . .env; set +a   # load GLM_API_KEY and GLM_BASE_URL
  python benchmarks/contextbench/run_glm_pass1.py --limit 10 --out glm_pass1.jsonl
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))

from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile

REPO_ROOT = os.path.join(_HERE, "..", "..", "benchmark_repos")
REPO_TO_LOCAL = {"django/django": "django", "psf/requests": "requests",
                 "pallets/flask": "flask"}
# Canonical full clones (no --filter). Created in Phase 1.
CANONICAL_CLONES = {
    "django": os.environ.get("DJANGO_CLONE_PATH",
                              "/home/trakshan/cb/django_canonical"),
    "requests": os.environ.get("REQUESTS_CLONE_PATH",
                                "/home/trakshan/cb/requests_canonical"),
    "flask": os.environ.get("FLASK_CLONE_PATH",
                            "/home/trakshan/cb/flask_canonical"),
}
SCRATCH_ROOT = os.environ.get("CB_SCRATCH_ROOT", "/home/trakshan/cb/scratch")
HTTP_TIMEOUT = 300
TEST_TIMEOUT = 300

# DiffContext commit SHA for provenance
DC_SHA = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=os.path.join(_HERE, "..", ".."),
    capture_output=True, text=True, timeout=5
).stdout.strip()[:12]


# ── git helpers (full stderr capture, never truncated) ─────────────────────

def _git(*args, cwd=None, timeout=120, env_extra=None):
    """Run git with GIT_TERMINAL_PROMPT=0 so network stalls fail fast.
    Returns (returncode, stdout, stderr) — stderr is NEVER truncated."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, timeout=timeout, env=env)
    return r.returncode, r.stdout, r.stderr


class ScratchClone:
    """A per-task local clone from the canonical full clone.

    No worktree registry, no shared object store, no prune. The clone is
    created fresh per task and removed in finally:. Independent .git/index
    means no concurrency corruption is possible.
    """

    def __init__(self, canonical_path, scratch_path, commit):
        self.canonical = os.path.abspath(canonical_path)
        self.path = scratch_path
        self.commit = commit
        if os.path.exists(scratch_path):
            import shutil
            shutil.rmtree(scratch_path, ignore_errors=True)
        os.makedirs(os.path.dirname(scratch_path), exist_ok=True)
        rc, out, err = _git("clone", "--local", "--no-hardlinks",
                           self.canonical, scratch_path, timeout=120)
        if rc != 0:
            raise RuntimeError(f"local clone failed (rc={rc}):\n{err}")
        rc, out, err = _git("checkout", "--detach", commit, cwd=scratch_path)
        if rc != 0:
            raise RuntimeError(f"checkout {commit[:12]} failed (rc={rc}):\n{err}")

    def reset_to(self, commit):
        """Reset the clone to a clean state at `commit` — discard ALL local
        changes, including ignored files. Asserts the postcondition so
        cross-variant contamination is caught, not silent."""
        # checkout -f --detach preserves the detached invariant; -f discards
        # tracked modifications. Does NOT move branch refs (unlike reset --hard).
        rc, out, err = _git("checkout", "-f", "--detach", commit, cwd=self.path)
        if rc != 0:
            raise RuntimeError(f"checkout -f --detach {commit[:12]} failed (rc={rc}):\n{err}")
        # clean -fdqx removes untracked AND ignored files (__pycache__, *.egg-info,
        # .pytest_cache) — stale artifacts are a silent cross-task channel.
        rc, out, err = _git("clean", "-fdqx", cwd=self.path)
        if rc != 0:
            raise RuntimeError(f"clean -fdqx failed (rc={rc}):\n{err}")
        # Postcondition — must hold before ANY variant runs.
        rc, head, _ = _git("rev-parse", "HEAD", cwd=self.path)
        if not head.strip().startswith(commit[:12]):
            raise AssertionError(f"reset_to postcondition: HEAD={head.strip()!r} != {commit}")
        rc, status, _ = _git("status", "--porcelain", cwd=self.path)
        if status.strip() != "":
            raise AssertionError(f"reset_to postcondition: dirty tree after reset:\n{status}")

    def provenance(self):
        """Return (head_sha, tree_porcelain) for logging into JSONL."""
        rc, head, _ = _git("rev-parse", "HEAD", cwd=self.path)
        rc2, status, _ = _git("status", "--porcelain", cwd=self.path)
        return head.strip()[:12], status.strip()

    def remove(self):
        import shutil
        shutil.rmtree(self.path, ignore_errors=True)


# ── gold-patch parsing ──────────────────────────────────────────────────────

def old_changed_ranges(patch):
    ranges = {}; cur = None
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]; continue
        if line.startswith("--- ") or not cur:
            continue
        if line.startswith("@@") and cur:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+", line)
            if not m or not cur.endswith(".py"):
                continue
            base = os.path.basename(cur)
            if base.startswith("test_") or base.endswith("_test.py") or "/tests/" in cur:
                continue
            a = int(m.group(1)); b = int(m.group(2)) if m.group(2) else 1
            lo, hi = (a, a) if b == 0 else (a, a + b - 1)
            ranges.setdefault(cur, []).append((lo, hi))
    return ranges


def seed_symbols(index, patch):
    ranges_by_file = old_changed_ranges(patch)
    if not ranges_by_file:
        return []
    by_file = {}
    for sid, sym in index.symbols.items():
        rel = sid.split(":", 1)[0]
        if rel.startswith("./"): rel = rel[2:]
        end = sym.lineno + max(len(sym.code.splitlines()) - 1, 0)
        by_file.setdefault(rel, []).append((sid, sym.lineno, end))
    seeds = []
    for f, ranges in ranges_by_file.items():
        for sid, lo, end in by_file.get(f, []):
            for rlo, rhi in ranges:
                if not (end < rlo or lo > rhi):
                    seeds.append(sid); break
    seen, out = set(), []
    for s in seeds:
        if s not in seen: seen.add(s); out.append(s)
    return out


def compile_context_text(index, seeds, max_tokens=8000, top_k=20, cutoff=None,
                         dep_boost=0.0):
    if not seeds:
        return "", 0
    impact = analyze_impact(index, seeds, hybrid=True, adaptive=True)
    pkg = dc_compile(index, impact, max_tokens=max_tokens, top_k=top_k,
                     cutoff=cutoff, dep_boost=dep_boost)
    return pkg.text, pkg.token_estimate


def compile_arm_context(index, provider, seeds, max_tokens=8000):
    """Shared-renderer context builder for the arms comparison (Task A).

    Seeds (the oracle floor — the functions the gold patch modifies) are
    rendered first and always included, IDENTICALLY for every arm. The
    remaining token budget is then filled with supporters ranked by
    `provider` via the shared render_context from
    benchmarks.downstream.providers — the same renderer trace_arms.py uses.

    The ONLY thing that differs between arms is which supporters are
    chosen: same seeds, same budget, same renderer, same model, same
    prompt. This is the falsification test — does DiffContext's hybrid
    selection beat BM25 / same-file selection downstream, or does any
    context do equally well?

    `diffcontext` here = rank_diffcontext (hybrid graph+BM25+samefile
    blend, recall-first, NO top_k cap, NO meta-header) under the shared
    renderer — NOT the product's rich dc_compile. That is deliberate: the
    rich meta-header is generated from DiffContext's own graph analysis,
    so giving it to one arm but not the others would confound the
    comparison. The published 21.9%/25.8% (context vs none) used the rich
    renderer and stand as a separate claim; this ledger isolates the
    selection algorithm under a controlled renderer.
    """
    from benchmarks.downstream.providers import (
        RANKERS, render_context, _estimate_tokens,
    )
    seeds_in_index = [s for s in seeds if s in index.symbols]
    if not seeds_in_index:
        return "", 0
    seed_parts, seed_chars = [], 0
    for sid in seeds_in_index:
        sym = index.symbols[sid]
        block = f"# {sid}\n{sym.code}\n"
        seed_chars += len(block) + (1 if seed_parts else 0)
        seed_parts.append(block)
    seed_text = "\n".join(seed_parts)
    seed_tokens = _estimate_tokens(seed_text)
    remaining = max(0, max_tokens - seed_tokens)
    ranked = RANKERS[provider](index, seeds_in_index)
    supporters = render_context(index, ranked, remaining)
    full = seed_text + ("\n\n" + supporters if supporters else "")
    return full, _estimate_tokens(full)


# ── patch application (full stderr) ─────────────────────────────────────────

def apply_patch(workdir, patch):
    """Apply a unified diff, trying a cascade of increasingly lenient tools.

    Returns (ok, detail, method). `method` names the command that succeeded
    (e.g. "git-apply-p1") or "" on failure — recorded in the JSONL so the
    apply-fix experiment can attribute recovered apply errors to the right
    fallback. The cascade is ordered strictest-first:

      1. git apply -p1
      2. git apply -p1 --recount              (recompute hunk line counts)
      3. git apply --3way                     (fall back to 3-way merge)
      4. git apply -p1 --ignore-whitespace     (tolerate whitespace drift)
      5. patch -p1 --fuzz=3                   (GNU patch, fuzzy context)
      6. patch -p1 --fuzz=3 <newline-normalized>  (trailing-newline fix:
         the dominant "ends in middle of line" failure is a missing final
         newline, not a token-cap truncation — see analyze_apply_errors.py)

    A non-empty diff with no trailing newline is normalized once and retried
    as the final fallback, so a clean model output is never rejected purely
    for missing the terminating newline.
    """
    if not patch or not patch.strip():
        return False, "empty patch", ""
    cascade = [
        ("git-apply-p1", ["git", "apply", "-p1"]),
        ("git-apply-recount", ["git", "apply", "-p1", "--recount"]),
        ("git-apply-3way", ["git", "apply", "--3way"]),
        ("git-apply-iws", ["git", "apply", "-p1", "--ignore-whitespace"]),
        ("patch-fuzz3", ["patch", "-p1", "--fuzz=3"]),
    ]
    last_err = ""
    for tag, cmd in cascade:
        r = subprocess.run(cmd, input=patch, cwd=workdir, capture_output=True,
                           text=True, timeout=30,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode == 0:
            return True, "", tag
        last_err = f"cmd={cmd[0]} rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    # Final fallback: normalize a missing trailing newline (the dominant
    # "ends in middle of line" cause) and retry with GNU patch.
    normalized = patch if patch.endswith("\n") else patch + "\n"
    if normalized != patch:
        r = subprocess.run(["patch", "-p1", "--fuzz=3"], input=normalized,
                           cwd=workdir, capture_output=True, text=True,
                           timeout=30,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode == 0:
            return True, "", "patch-newline-fix"
        last_err = (f"cmd=patch(rc={r.returncode}, newline-fix) "
                    f"stdout={r.stdout} stderr={r.stderr}")
    return False, last_err, ""


# ── test running ───────────────────────────────────────────────────────────

def _f2p_to_django_labels(f2p):
    """Convert ContextBench f2p format to Django test labels."""
    out = []
    for t in f2p:
        m = re.match(r"(\S+)\s+\((.+)\)", t)
        out.append(f"{m.group(2)}.{m.group(1)}" if m else t)
    return out


def run_tests(workdir, f2p, test_python, repo, timeout=TEST_TIMEOUT):
    if not f2p:
        return False, -1, "no f2p tests"
    py = test_python or sys.executable
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([workdir, os.path.join(workdir, "src")])
    env.pop("PYTEST_ADDOPTS", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if repo == "django":
        labels = _f2p_to_django_labels(f2p)
        tests_dir = os.path.join(workdir, "tests")
        r = subprocess.run([py, "runtests.py"] + labels +
                          ["--settings=test_sqlite", "--verbosity=1"],
                          cwd=tests_dir, capture_output=True, text=True,
                          timeout=timeout, env=env)
    else:
        env["DJANGO_SETTINGS_MODULE"] = "tests.test_sqlite"
        r = subprocess.run([py, "-m", "pytest", "-x", "-q", "--no-header",
                          "-p", "no:cacheprovider", *f2p],
                          cwd=workdir, capture_output=True, text=True,
                          timeout=timeout, env=env)
    passed = r.returncode == 0
    output = (r.stdout + r.stderr) if not passed else ""
    return passed, r.returncode, output


# ── GLM generation ──────────────────────────────────────────────────────────

def _ensure_trailing_newline(diff):
    """git apply / patch require a final newline. glm_generate used to return
    diff.strip(), which drops it — the dominant cause of "patch unexpectedly
    ends in middle of line" apply errors (65% of all apply errors; see
    analyze_apply_errors.py: 134/143 truncated diffs are short, finish=stop,
    NOT token-limited). Preserve exactly one trailing newline."""
    if not diff:
        return diff
    return diff.rstrip("\n") + "\n"


def _extract_diff_from_reasoning(reasoning_text):
    """Salvage parser: extract a diff from reasoning_content when content
    is null (finish_reason=length). The model reasoned to an answer but was
    cut off before emitting it as content. Never launder this as a clean
    PASS — label it via content_source='reasoning' in the JSONL."""
    # Try fenced diff block first
    m = re.search(r"```(?:diff|patch)?\n(.*?)```", reasoning_text, re.DOTALL)
    if m:
        diff = m.group(1).strip()
        if diff.startswith("diff --git") or diff.startswith("---"):
            return _ensure_trailing_newline(diff)
    # Try unfenced --- a/ ... +++ b/ block
    m = re.search(r"(diff --git .*?)(?=\n\n[A-Z]|\n```|\Z)", reasoning_text, re.DOTALL)
    if m:
        return _ensure_trailing_newline(m.group(1).strip())
    # Try --- a/ ... +++ b/ pattern
    lines = reasoning_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("--- a/") or line.startswith("diff --git"):
            start = i
            break
    if start is not None:
        # Collect until we hit non-diff content
        diff_lines = []
        for line in lines[start:]:
            if line.startswith("--- a/") or line.startswith("+++ b/") or \
               line.startswith("@@") or line.startswith(" ") or \
               line.startswith("+") or line.startswith("-") or \
               line.startswith("diff --git") or line.startswith("index "):
                diff_lines.append(line)
            elif diff_lines and not line.strip():
                # Allow one blank line inside diff
                diff_lines.append(line)
            elif diff_lines:
                break
        if diff_lines:
            return _ensure_trailing_newline("\n".join(diff_lines).strip())
    return ""


def glm_generate(api_key, base_url, model, problem, context, test_hint):
    """Generate a patch from GLM 5.2. Returns (diff_str, error_str, meta_dict).

    Uses chat_template_kwargs: {"enable_thinking": false} to suppress
    reasoning. Confirmed by discriminating tests (see /tmp/glm_discriminating.log):
    - reasoning_effort: "none"  -> 82.7s, NULL content, 68k reasoning (dropped)
    - chat_template_kwargs      -> 7.7s, 6332c content, 0c reasoning (works)
    - extra_body thinking       -> 4.7s, 2368c content, 0c reasoning (works)

    Falls back to parsing reasoning_content if content is null (salvage parser).
    """
    system = ("You are an expert software engineer fixing a bug. "
              "Think briefly about the root cause, then output ONLY a unified "
              "diff (git diff format) that fixes it. Do not over-explain. "
              "Use the exact file paths from the provided context.")
    user = (f"## Problem\n{problem}\n\n"
            f"## Failing tests (must pass after your fix)\n{test_hint}\n\n"
            f"## Relevant code context\n{context}\n\n"
            "## Task\nProduce a unified diff (git diff format) that fixes the "
            "issue so the failing tests pass. Output only the diff.")
    body = {"model": model, "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}],
            "max_tokens": 16384, "temperature": 0.0,
            # Suppress reasoning — confirmed working via discriminating tests.
            "chat_template_kwargs": {"enable_thinking": False}}
    import requests as req
    meta = {}
    for attempt in range(3):
        if attempt > 0:
            time.sleep(8)
        r = req.post(f"{base_url}/chat/completions",
                     headers={"Authorization": f"Bearer {api_key}",
                              "Content-Type": "application/json"},
                     json=body, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return "", f"HTTP {r.status_code}: {r.text[:500]}", meta
        d = r.json()
        msg = d["choices"][0]["message"]
        content = msg.get("content")
        reasoning = msg.get("reasoning_content") or ""
        finish = d["choices"][0].get("finish_reason", "?")
        usage = d.get("usage", {})
        meta = {
            "finish_reason": finish,
            "reasoning_len": len(reasoning),
            "completion_tokens": usage.get("completion_tokens", 0),
            "content_source": None,
        }
        if content is not None:
            meta["content_source"] = "content"
            break
        # Salvage parser: try to extract a diff from reasoning_content.
        print(f"    [glm] null content (attempt {attempt+1}/3): "
              f"finish={finish} reasoning={len(reasoning)}c usage={usage}",
              file=sys.stderr)
        diff_from_reasoning = _extract_diff_from_reasoning(reasoning)
        if diff_from_reasoning:
            meta["content_source"] = "reasoning"
            return diff_from_reasoning, "", meta
    if content is None:
        return "", "GLM returned null content after 3 retries", meta
    m = re.search(r"```(?:diff|patch)?\n(.*?)```", content, re.DOTALL)
    diff = m.group(1) if m else content
    if not diff.strip().startswith("diff --git") and not diff.strip().startswith("---"):
        return "", "no diff in output", meta
    return _ensure_trailing_newline(diff.strip()), "", meta


# ── checkpoint/resume ───────────────────────────────────────────────────────

def load_done(out_path):
    """Load completed (instance_id, variant) pairs from an existing JSONL."""
    done = set()
    if not os.path.isfile(out_path):
        return done
    with open(out_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                key = (d.get("instance_id", ""), d.get("variant", ""))
                if key[0]:
                    done.add(key)
            except Exception:
                pass
    return done


def append_result(out_path, rec):
    """Append one result to the JSONL (checkpoint-safe)."""
    with open(out_path, "a") as f:
        f.write(json.dumps(rec) + "\n")


# ── main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="glm_pass1.jsonl")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--repos", default="django")
    ap.add_argument("--model", default="glm-5.2")
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--variants", default="diffcontext,diffcontext_gap")
    ap.add_argument("--test-python", default="",
                    help="Python to use for running tests (default: sys.executable)")
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip GLM generation — validate setup + context compile only. "
                         "No API key needed. For smoke testing in CI.")
    ap.add_argument("--only-instances", default="",
                    help="Comma-separated instance_ids to run (subset filter). "
                         "Empty = all matching tasks. Used for targeted re-runs "
                         "of apply-error cases with diff capture.")
    ap.add_argument("--shared-renderer", action="store_true",
                    help="Arms-comparison mode (Task A): route diffcontext, "
                         "diffcontext_gap, bm25, and samefile through ONE "
                         "shared renderer (seeds + render_context) so the "
                         "ONLY difference between arms is supporter ranking. "
                         "diffcontext here is rank_diffcontext under the "
                         "shared renderer, NOT the product's rich dc_compile. "
                         "bm25/samefile are only meaningful with this flag.")
    args = ap.parse_args()
    test_py = args.test_python or sys.executable

    # Load .env if env vars are not set
    if not os.environ.get("GLM_API_KEY"):
        env_path = os.path.join(_HERE, "..", "..", ".env")
        if os.path.isfile(env_path):
            for line in open(env_path):
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    os.environ.setdefault(k, v)
    api_key = os.environ.get("GLM_API_KEY", "")
    base_url = os.environ.get("GLM_BASE_URL", "")
    if not args.no_llm and (not api_key or not base_url):
        sys.exit("GLM_API_KEY and GLM_BASE_URL must be set (env or .env). "
                 "Use --no-llm for setup-only smoke testing.")

    os.makedirs(SCRATCH_ROOT, exist_ok=True)
    variants = args.variants.split(",")
    wanted = set(args.repos.split(","))
    done = load_done(args.out)

    from datasets import load_dataset
    ds = load_dataset("Contextbench/ContextBench", "default", split="train",
                      revision="c2855792b006af41c67202d33883fb9d46362853")
    tasks = [r for r in ds if r["language"] == "python" and r["repo"] in REPO_TO_LOCAL
             and REPO_TO_LOCAL[r["repo"]] in wanted]
    # Print the true denominator before any slicing — settles 6-vs-7 etc.
    from collections import Counter
    print(f"filtered tasks: {len(tasks)} (language=python, repos={sorted(wanted)})")
    print(f"per-repo: {dict(Counter(REPO_TO_LOCAL[r['repo']] for r in tasks))}")
    # Optional instance subset (targeted re-run of apply-error cases).
    if args.only_instances:
        want_ids = {s.strip() for s in args.only_instances.split(",") if s.strip()}
        tasks = [r for r in tasks if r["instance_id"] in want_ids]
        print(f"--only-instances: {len(tasks)}/{len(want_ids)} requested matched")
        missing = want_ids - {r["instance_id"] for r in tasks}
        if missing:
            print(f"  WARNING: {len(missing)} requested ids not in dataset: "
                  f"{sorted(missing)[:3]}{'...' if len(missing) > 3 else ''}")
    if not tasks:
        sys.exit(f"ERROR: no tasks matched filter (language=python, repos={sorted(wanted)}). "
                 f"Exiting non-zero — silence-on-empty hides infrastructure failures.")
    if args.limit > 0:
        tasks = tasks[:args.limit]

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(f"diffcontext_sha={DC_SHA} model={args.model} timestamp={now}")
    print(f"variants={variants} repos={args.repos} limit={args.limit}")
    print(f"resume: {len(done)} (instance,variant) pairs already done")

    for row in tasks:
        iid = row["instance_id"]
        commit = row["base_commit"]
        local = REPO_TO_LOCAL[row["repo"]]
        canonical = CANONICAL_CLONES.get(local, os.path.join(REPO_ROOT, local))
        if not os.path.isdir(os.path.join(canonical, ".git")):
            print(f"[skip] {iid}: no canonical clone at {canonical}")
            continue
        f2p = json.loads(row["f2p"]) if row["f2p"] else []
        if not f2p:
            print(f"[skip] {iid}: no f2p tests")
            continue
        test_patch = row["test_patch"]
        problem = row["problem_statement"][:2000]
        test_hint = "\n".join(f2p[:10])

        # Check which variants still need running
        pending = [v for v in variants if (iid, v) not in done]
        if not pending:
            print(f"[skip] {iid}: all variants already done")
            continue

        # ── Setup: clone + index + compile contexts (once per task) ───────
        # error_class taxonomy:
        #   null          — success (or skipped_no_llm smoke test)
        #   setup_error   — git/clone/patch infrastructure failure
        #   no_seeds      — methodology limitation (oracle miss: gold patch
        #                   touches class/module-level code, not functions)
        #   gen_error     — LLM generation failure (null content, HTTP error)
        #   apply_error   — patch application failure
        #   test_error    — tests ran but did not pass
        #   skipped_no_llm — --no-llm smoke test (not an error)
        scratch = None
        setup_ec = None       # "setup_error" | "no_seeds" | None
        setup_detail = None   # human-readable detail string
        seeds = []
        ctxs = {}
        ctx_tokens = {}
        try:
            scratch_path = os.path.join(SCRATCH_ROOT, f"{iid}_{os.getpid()}")
            scratch = ScratchClone(canonical, scratch_path, commit)
            ok, err, _method = apply_patch(scratch.path, test_patch)
            if not ok:
                setup_ec = "setup_error"
                setup_detail = f"test_patch apply failed:\n{err}"
            else:
                index = index_repository(scratch.path)
                seeds = seed_symbols(index, row["patch"])
                if not seeds:
                    setup_ec = "no_seeds"
                    setup_detail = ("no function-level seeds extracted "
                                    "from gold patch (oracle miss)")
                else:
                    for v in pending:
                        if v == "none":
                            ctxs[v] = ""; ctx_tokens[v] = 0
                        elif args.shared_renderer and v in (
                            "diffcontext", "diffcontext_gap", "bm25", "samefile"
                        ):
                            ctxs[v], ctx_tokens[v] = compile_arm_context(
                                index, v, seeds, args.max_tokens)
                        elif v == "diffcontext":
                            ctxs[v], ctx_tokens[v] = compile_context_text(
                                index, seeds, args.max_tokens)
                        elif v == "diffcontext_gap":
                            ctxs[v], ctx_tokens[v] = compile_context_text(
                                index, seeds, args.max_tokens, cutoff="gap")
                        elif v == "diffcontext_depboost":
                            ctxs[v], ctx_tokens[v] = compile_context_text(
                                index, seeds, args.max_tokens, cutoff="gap",
                                dep_boost=20.0)
                        else:
                            ctxs[v] = ""; ctx_tokens[v] = 0
        except Exception as e:
            setup_ec = "setup_error"
            setup_detail = f"{type(e).__name__}: {e}"

        if setup_ec:
            if scratch:
                scratch.remove()
            # Always emit one row per (instance_id, variant) — matched pairs
            # are required for paired statistics (sign test, bootstrap CI).
            for v in pending:
                rec = {
                    "instance_id": iid, "variant": v, "repo": local,
                    "diffcontext_sha": DC_SHA, "model": args.model,
                    "timestamp_utc": now,
                    "error_class": setup_ec,
                    "error_detail": setup_detail,
                    "n_seeds": len(seeds), "ctx_tokens": None, "ctx_len": None,
                    "f2p_tests": f2p, "passed": None, "test_exit_code": None,
                    "head_sha": None, "tree_status": None,
                }
                append_result(args.out, rec)
                done.add((iid, v))
            print(f"[{local}] {iid}: {setup_ec.upper()}: {setup_detail[:120]}")
            continue

        # ── Per-variant: reset clone → generate → apply → test ──────────
        # Reuse the SAME clone (reset between variants) — no new clones.
        for variant in pending:
            t0 = time.perf_counter()
            rec = {
                "instance_id": iid, "variant": variant, "repo": local,
                "diffcontext_sha": DC_SHA, "model": args.model,
                "timestamp_utc": now,
                "n_seeds": len(seeds),
                "ctx_tokens": ctx_tokens.get(variant),
                "ctx_len": len(ctxs.get(variant, "")),
                "f2p_tests": f2p,
                "passed": None, "test_exit_code": None,
                "error_class": None, "error_detail": None,
                "head_sha": None, "tree_status": None,
                "content_source": None, "finish_reason": None,
                "reasoning_len": None, "completion_tokens": None,
                "apply_method": None, "diff_len": None, "diff": None,
            }
            try:
                # Reset the clone to clean base_commit, re-apply test_patch.
                scratch.reset_to(commit)
                head_sha, tree_status = scratch.provenance()
                rec["head_sha"] = head_sha
                rec["tree_status"] = tree_status
                ok, err, _method = apply_patch(scratch.path, test_patch)
                if not ok:
                    rec["error_class"] = "setup_error"
                    rec["error_detail"] = f"test_patch re-apply:\n{err}"
                    continue
                if args.no_llm:
                    # Setup-only smoke test: no GLM, no apply, no tests.
                    rec["error_class"] = "skipped_no_llm"
                    rec["error_detail"] = "--no-llm mode: setup + context compile only"
                    continue
                diff, err, gen_meta = glm_generate(api_key, base_url, args.model,
                                        problem, ctxs.get(variant) or "(no additional context)",
                                        test_hint)
                rec["content_source"] = gen_meta.get("content_source")
                rec["finish_reason"] = gen_meta.get("finish_reason")
                rec["reasoning_len"] = gen_meta.get("reasoning_len")
                rec["completion_tokens"] = gen_meta.get("completion_tokens")
                if err:
                    rec["error_class"] = "gen_error"
                    rec["error_detail"] = err
                    continue
                rec["diff_len"] = len(diff)
                ok, err, apply_method = apply_patch(scratch.path, diff)
                rec["applied"] = ok
                rec["apply_method"] = apply_method or None
                # Capture the generated diff (truncated) so apply errors can be
                # diagnosed without re-running the LLM. Essential for the
                # Phase 1 malformed-diff analysis (see analyze_apply_errors.py).
                rec["diff"] = diff[:8000]
                if not ok:
                    rec["error_class"] = "apply_error"
                    rec["error_detail"] = err
                    continue
                passed, exit_code, output = run_tests(
                    scratch.path, f2p, test_py, local)
                rec["passed"] = passed
                rec["test_exit_code"] = exit_code
                if not passed:
                    rec["error_class"] = "test_error"
                    rec["error_detail"] = output[-2000:] if output else ""
            except subprocess.TimeoutExpired as e:
                rec["error_class"] = "test_error"
                rec["error_detail"] = f"TIMEOUT after {e.timeout}s"
            except Exception as e:
                rec["error_class"] = "setup_error"
                rec["error_detail"] = f"unexpected: {type(e).__name__}: {e}"
            finally:
                dt = time.perf_counter() - t0
                rec["sec"] = round(dt, 1)
                append_result(args.out, rec)
                done.add((iid, variant))
                verdict = "PASS" if rec.get("passed") else rec.get("error_class", "FAIL")
                print(f"[{local}] {iid} {variant}: {verdict} ({dt:.1f}s) "
                      f"seeds={len(seeds)} ctx_tok={ctx_tokens.get(variant, 0)}")
                time.sleep(5)

        # Remove the clone after all variants.
        if scratch:
            scratch.remove()

    # ── Summary ─────────────────────────────────────────────────────────
    all_results = []
    if os.path.isfile(args.out):
        with open(args.out) as f:
            all_results = [json.loads(l) for l in f if l.strip()]

    if args.no_llm:
        # --no-llm mode: print seed census, NOT pass rate (nothing generated).
        print("\n=== SEED CENSUS (--no-llm) ===")
        from collections import defaultdict
        by_repo = defaultdict(lambda: {"total": 0, "with_seeds": 0,
                                        "no_seeds": 0, "setup_err": 0})
        for r in all_results:
            repo = r.get("repo", "?")
            by_repo[repo]["total"] += 1
            ec = r.get("error_class")
            if ec == "no_seeds":
                by_repo[repo]["no_seeds"] += 1
            elif ec == "setup_error":
                by_repo[repo]["setup_err"] += 1
            elif r.get("n_seeds", 0) > 0:
                by_repo[repo]["with_seeds"] += 1
        print(f"{'repo':<12} {'total':>5} {'seeds':>5} {'no_seed':>7} {'setup_err':>9}")
        print(f"{'-'*12} {'-'*5} {'-'*5} {'-'*7} {'-'*9}")
        for repo in sorted(by_repo):
            d = by_repo[repo]
            print(f"{repo:<12} {d['total']:>5} {d['with_seeds']:>5} "
                  f"{d['no_seeds']:>7} {d['setup_err']:>9}")
        tot = {k: sum(by_repo[r][k] for r in by_repo) for k in ("total", "with_seeds", "no_seeds", "setup_err")}
        print(f"{'TOTAL':<12} {tot['total']:>5} {tot['with_seeds']:>5} "
              f"{tot['no_seeds']:>7} {tot['setup_err']:>9}")
    else:
        print("\n=== PASS@1 SUMMARY ===")
        for v in variants:
            vr = [r for r in all_results if r.get("variant") == v]
            passed = sum(1 for r in vr if r.get("passed") is True)
            no_seeds = sum(1 for r in vr if r.get("error_class") == "no_seeds")
            setup_err = sum(1 for r in vr if r.get("error_class") == "setup_error")
            gen_err = sum(1 for r in vr if r.get("error_class") == "gen_error")
            apply_err = sum(1 for r in vr if r.get("error_class") == "apply_error")
            test_fail = sum(1 for r in vr if r.get("passed") is False
                            and r.get("error_class") not in ("setup_error", "no_seeds"))
            # "attempted" = rows where we actually tried LLM generation
            non_evaluable = ("setup_error", "no_seeds", "skipped_no_llm")
            attempted = sum(1 for r in vr if r.get("error_class") not in non_evaluable)
            print(f"  {v}: {passed}/{len(vr)} passed | "
                  f"attempted={attempted} no_seeds={no_seeds} "
                  f"setup_err={setup_err} gen_err={gen_err} "
                  f"apply_err={apply_err} test_fail={test_fail}")


if __name__ == "__main__":
    main()
