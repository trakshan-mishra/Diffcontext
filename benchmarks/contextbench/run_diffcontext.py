#!/usr/bin/env python3
"""
run_diffcontext.py — run DiffContext as a context-retrieval method on
ContextBench and emit trajectories in ContextBench's unified format.

For each ContextBench Python task in the repos we have cloned locally
(django, requests, flask), this script:

  1. checks out the task's base_commit in a git worktree,
  2. indexes the repo at that state with DiffContext,
  3. extracts oracle seed symbols from the gold `patch` (the functions the
     gold fix modifies — the same oracle-localization methodology as
     DiffContext's own downstream eval, disclosed as a limitation),
  4. runs DiffContext hybrid retrieval (analyze_impact + compile) into a
     token budget, and
  5. emits one trajectory row per task in ContextBench's unified format,
     for THREE variants so the retrieval contribution is separable from
     the oracle-localization floor:

       seeds_only          — just the gold-changed functions (the floor:
                             what oracle localization gives for free)
       retrieved_only      — DiffContext's retrieved supporters, excluding
                             the seeds (DiffContext's pure contribution)
       seeds_plus_retrieved— the full context window handed to the model
                             (seeds + retrieved); the primary headline

The emitted JSONL is scored by the official ContextBench evaluator
(`python -m contextbench.evaluate`) for file/symbol/span/line coverage
and precision against the human-annotated gold_context.

No LLM is used here — this is pure retrieval vs gold. Downstream pass@1
with GLM 5.2 is a separate script (run_glm_pass1.py).

Usage:
  python benchmarks/contextbench/run_diffcontext.py \
      --out-dir benchmarks/contextbench/runs \
      --max-tokens 8000 --top-k 20
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

# DiffContext lives in the parent repo; make it importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))

from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile
from diffcontext.models import RepositoryIndex, Symbol

# Repos we have cloned locally that intersect ContextBench's Python set.
REPO_ROOT = os.path.join(_HERE, "..", "..", "benchmark_repos")
# ContextBench `repo` field -> local clone directory name.
REPO_TO_LOCAL = {
    "django/django": "django",
    "psf/requests": "requests",
    "pallets/flask": "flask",
}
# Canonical FULL clones (no --filter, non-shallow). The benchmark_repos/
# clones are shallow and lack the base_commit SHAs, so worktree add fails;
# these have the full history. Same convention as run_glm_pass1.py.
CANONICAL_CLONES = {
    "django": os.environ.get("DJANGO_CLONE_PATH",
                             "/home/trakshan/cb/django_canonical"),
    "requests": os.environ.get("REQUESTS_CLONE_PATH",
                                "/home/trakshan/cb/requests_canonical"),
    "flask": os.environ.get("FLASK_CLONE_PATH",
                            "/home/trakshan/cb/flask_canonical"),
}


# ── git worktree (one per repo, reused across commits) ──────────────────────

class Worktree:
    def __init__(self, repo: str, path: str, sha: str):
        self.repo = os.path.abspath(repo)
        self.path = path
        if os.path.exists(path):
            self.remove()
        r = self._git("worktree", "add", "--detach", path, sha, cwd=self.repo)
        if r.returncode != 0:
            raise RuntimeError(f"worktree add failed: {r.stderr.strip()}")

    def checkout(self, sha: str) -> None:
        for cmd in (("checkout", "-f", "--detach", sha), ("clean", "-fdqx")):
            r = self._git(*cmd, cwd=self.path)
            if r.returncode != 0:
                raise RuntimeError(f"git {cmd[0]} failed: {r.stderr.strip()}")
        # Postcondition — catches cross-task contamination before it silently
        # degrades a variant (same invariant as ScratchClone.reset_to).
        r = self._git("rev-parse", "HEAD", cwd=self.path)
        if not r.stdout.strip().startswith(sha[:12]):
            raise AssertionError(
                f"checkout postcondition: HEAD={r.stdout.strip()!r} != {sha}")

    def remove(self) -> None:
        self._git("worktree", "remove", "--force", self.path, cwd=self.repo)
        import shutil
        shutil.rmtree(self.path, ignore_errors=True)
        self._git("worktree", "prune", cwd=self.repo)

    @staticmethod
    def _git(*args: str, cwd: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)


# ── gold-patch parsing: base_commit (old) line ranges per non-test file ───────

def old_changed_ranges(patch: str) -> Dict[str, List[Tuple[int, int]]]:
    """Parse a unified diff into {file: [(start_line, end_line)]} for the
    OLD (base_commit) side. Ranges are inclusive, 1-indexed. Non-test .py
    files only (the production code the gold fix modifies = oracle seeds)."""
    ranges: Dict[str, List[Tuple[int, int]]] = {}
    cur_file: Optional[str] = None
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            cur_file = line[6:]
            continue
        if line.startswith("--- "):
            continue  # /dev/null or a/...; we track file via +++
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m or cur_file is None:
                continue
            if not cur_file.endswith(".py"):
                continue
            base = os.path.basename(cur_file)
            is_test = (base.startswith("test_") or base.endswith("_test.py")
                       or "/tests/" in cur_file or cur_file.startswith("tests/"))
            if is_test:
                continue
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) is not None else 1
            if b == 0:
                # pure insertion (no old lines); probe the insertion anchor.
                lo = hi = a
            else:
                lo, hi = a, a + b - 1
            ranges.setdefault(cur_file, []).append((lo, hi))
    return ranges


def seed_symbols_from_patch(index: RepositoryIndex, patch: str) -> List[str]:
    """Map the gold patch's old-side changed lines to DiffContext symbol IDs
    at base_commit. A symbol is a seed if its [lineno, end] overlaps any
    changed range in its file."""
    ranges_by_file = old_changed_ranges(patch)
    if not ranges_by_file:
        return []
    # Build file -> [(sid, lineno, end)] once from the index.
    by_file: Dict[str, List[Tuple[str, int, int]]] = {}
    for sid, sym in index.symbols.items():
        rel = sid.split(":", 1)[0]  # "./rel/path.py"
        if rel.startswith("./"):
            rel = rel[2:]
        end = sym.lineno + max(len(sym.code.splitlines()) - 1, 0)
        by_file.setdefault(rel, []).append((sid, sym.lineno, end))
    seeds: List[str] = []
    for f, ranges in ranges_by_file.items():
        for sid, lo, end in by_file.get(f, []):
            for rlo, rhi in ranges:
                if not (end < rlo or lo > rhi):  # overlap
                    seeds.append(sid)
                    break
    # Dedupe, preserve order.
    seen, out = set(), []
    for s in seeds:
        if s not in seen:
            seen.add(s); out.append(s)
    return out


# ── symbol -> span ───────────────────────────────────────────────────────────

def sym_to_span(index: RepositoryIndex, sid: str) -> Optional[Tuple[str, int, int]]:
    sym = index.symbols.get(sid)
    if sym is None:
        return None
    rel = sid.split(":", 1)[0]
    if rel.startswith("./"):
        rel = rel[2:]
    end = sym.lineno + max(len(sym.code.splitlines()) - 1, 0)
    return (rel, sym.lineno, end)


def spans_dict_from_syms(index: RepositoryIndex, sids: List[str]) -> Dict[str, List[dict]]:
    """{file: [{"start": s, "end": e}, ...]} — the pred_spans format the
    ContextBench trajectory parser expects (keys are 'start'/'end')."""
    out: Dict[str, List[dict]] = {}
    for sid in sids:
        sp = sym_to_span(index, sid)
        if sp is None:
            continue
        f, s, e = sp
        out.setdefault(f, []).append({"start": s, "end": e})
    # Merge overlapping spans per file for clean, non-double-counted regions.
    for f in out:
        iv = sorted(out[f], key=lambda x: x["start"])
        merged = [dict(iv[0])]
        for cur in iv[1:]:
            last = merged[-1]
            if cur["start"] <= last["end"] + 1:
                last["end"] = max(last["end"], cur["end"])
            else:
                merged.append(dict(cur))
        out[f] = merged
    return out


def files_from_spans(spans: Dict[str, List[dict]]) -> List[str]:
    return sorted(spans.keys())


# ── quick self-metric (line-level recall/precision vs gold_context) ──────────

def gold_line_spans(gold_context: str) -> Dict[str, List[Tuple[int, int]]]:
    """{file: [(start_line, end_line)]} from the dataset's gold_context."""
    try:
        items = json.loads(gold_context)
    except Exception:
        return {}
    out: Dict[str, List[Tuple[int, int]]] = {}
    for it in items:
        f = it.get("file", "").replace("\\", "/")
        if f.startswith("/"):
            f = f.lstrip("/")
        f = f.lstrip("./")
        if not f:
            continue
        out.setdefault(f, []).append((int(it.get("start_line", 1)), int(it.get("end_line", 1))))
    # merge
    for f in out:
        iv = sorted(out[f])
        merged = [iv[0]]
        for cur in iv[1:]:
            last = merged[-1]
            if cur[0] <= last[1] + 1:
                merged[-1] = (last[0], max(last[1], cur[1]))
            else:
                merged.append(cur)
        out[f] = merged
    return out


def _line_len(d: Dict[str, List[Tuple[int, int]]]) -> int:
    return sum(e - s + 1 for iv in d.values() for s, e in iv)


def _line_inter(a: Dict[str, List[Tuple[int, int]]],
                b: Dict[str, List[Tuple[int, int]]]) -> int:
    total = 0
    for f in set(a) | set(b):
        ai = sorted(a.get(f, [])); bi = sorted(b.get(f, []))
        i = j = 0
        while i < len(ai) and j < len(bi):
            lo = max(ai[i][0], bi[j][0]); hi = min(ai[i][1], bi[j][1])
            if lo <= hi:
                total += hi - lo + 1
            if ai[i][1] < bi[j][1]:
                i += 1
            elif bi[j][1] < ai[i][1]:
                j += 1
            else:
                i += 1; j += 1
    return total


def self_metrics(pred_spans: Dict[str, List[dict]], gold_ctx: str) -> dict:
    """Line-level recall/precision/F1 — an immediate sanity check before the
    official evaluator run (which also gives file/symbol/span granularities)."""
    pred = {f: [(s["start"], s["end"]) for s in ivs] for f, ivs in pred_spans.items()}
    gold = gold_line_spans(gold_ctx)
    p = _line_len(pred); g = _line_len(gold); inter = _line_inter(pred, gold)
    rec = inter / g if g > 0 else 1.0
    prec = inter / p if p > 0 else 1.0
    f1 = 2 * rec * prec / (rec + prec) if (rec + prec) > 0 else 0.0
    # file-level too
    pf = set(pred); gf = set(gold)
    fi = len(pf & gf)
    frec = fi / len(gf) if gf else 1.0
    fprec = fi / len(pf) if pf else 1.0
    return {"line_recall": round(rec, 4), "line_precision": round(prec, 4),
            "line_f1": round(f1, 4), "file_recall": round(frec, 4),
            "file_precision": round(fprec, 4), "pred_lines": p, "gold_lines": g}


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="benchmarks/contextbench/runs")
    ap.add_argument("--max-tokens", type=int, default=8000,
                    help="context token budget for compile (the LLM window)")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0, help="cap tasks per repo (0=all)")
    ap.add_argument("--repos", default="", help="comma-sep local dir names to include (default: all)")
    args = ap.parse_args()
    if not args.out_dir or not args.max_tokens:
        ap.error("bad args")
    os.makedirs(args.out_dir, exist_ok=True)

    wanted = set(args.repos.split(",")) if args.repos else set(REPO_TO_LOCAL.values())

    from datasets import load_dataset
    ds = load_dataset("Contextbench/ContextBench", "default", split="train",
                      revision="c2855792b006af41c67202d33883fb9d46362853")
    tasks = [r for r in ds
             if r["language"] == "python" and r["repo"] in REPO_TO_LOCAL
             and REPO_TO_LOCAL[r["repo"]] in wanted]
    # group by local repo so we reuse one worktree per repo
    by_repo: Dict[str, List[dict]] = {}
    for r in tasks:
        by_repo.setdefault(REPO_TO_LOCAL[r["repo"]], []).append(r)
    for rep in by_repo:
        by_repo[rep].sort(key=lambda r: r["instance_id"])

    pred_files = {
        "seeds_only": open(os.path.join(args.out_dir, "pred_seeds_only.jsonl"), "w"),
        "retrieved_only": open(os.path.join(args.out_dir, "pred_retrieved_only.jsonl"), "w"),
        "seeds_plus_retrieved": open(os.path.join(args.out_dir, "pred_seeds_plus_retrieved.jsonl"), "w"),
        "diffcontext_gap": open(os.path.join(args.out_dir, "pred_diffcontext_gap.jsonl"), "w"),
    }
    summary = []
    wt: Optional[Worktree] = None
    try:
        for local_repo, rows in by_repo.items():
            repo_path = CANONICAL_CLONES.get(local_repo,
                                             os.path.join(REPO_ROOT, local_repo))
            if not os.path.isdir(os.path.join(repo_path, ".git")):
                print(f"[skip] {local_repo}: no clone at {repo_path}")
                continue
            count = 0
            for row in rows:
                if args.limit and count >= args.limit:
                    break
                iid = row["instance_id"]
                commit = row["base_commit"]
                t0 = time.perf_counter()
                try:
                    if wt is None or wt.repo != os.path.abspath(repo_path):
                        if wt is not None:
                            wt.remove()
                        wt = Worktree(repo_path,
                                      os.path.abspath(os.path.join(
                                          args.out_dir, f"wt_{local_repo}")),
                                      commit)
                    else:
                        wt.checkout(commit)
                    index = index_repository(wt.path)
                    seeds = seed_symbols_from_patch(index, row["patch"])
                    retrieved: List[str] = []
                    retrieved_gap: List[str] = []
                    ctx_tokens = 0
                    if seeds:
                        impact = analyze_impact(index, seeds, hybrid=True, adaptive=True)
                        # Rank retrieved supporters (exclude seeds), take top-k.
                        seed_set = set(seeds)
                        ranked = sorted(
                            ((sid, sc) for sid, sc in impact.scores.items()
                             if sid not in seed_set and sid in index.symbols),
                            key=lambda x: x[1], reverse=True)
                        pkg = dc_compile(index, impact, max_tokens=args.max_tokens,
                                         top_k=args.top_k)
                        selected = [it.symbol_id for it in (pkg.items or [])]
                        retrieved = [s for s in selected if s not in seed_set]
                        ctx_tokens = getattr(pkg, "token_estimate", 0)
                        # Precision variant: cutoff="gap" — cut at the
                        # largest relative score drop (the precision lever).
                        pkg_gap = dc_compile(index, impact, max_tokens=args.max_tokens,
                                             top_k=args.top_k, cutoff="gap")
                        retrieved_gap = [it.symbol_id for it in (pkg_gap.items or [])
                                         if it.symbol_id not in seed_set]
                    # Build the variant span sets.
                    variants = {
                        "seeds_only": seeds,
                        "retrieved_only": retrieved,
                        "seeds_plus_retrieved": seeds + retrieved,
                        "diffcontext_gap": seeds + retrieved_gap,
                    }
                    row_out = {"instance_id": iid, "repo_url": repo_path,
                               "commit": commit,
                               "source": row["source"], "repo": row["repo"]}
                    sm = {}
                    for vname, sids in variants.items():
                        spans = spans_dict_from_syms(index, sids)
                        rec = {"traj_data": {
                                "pred_steps": [{
                                    "files": files_from_spans(spans),
                                    "spans": {f: [{"start": s["start"], "end": s["end"]} for s in ivs]
                                              for f, ivs in spans.items()},
                                    "symbols": {}}],
                                "pred_files": files_from_spans(spans),
                                "pred_spans": {f: [{"start": s["start"], "end": s["end"]} for s in ivs]
                                               for f, ivs in spans.items()},
                              }, "model_patch": ""}
                        pred_files[vname].write(json.dumps({**row_out, **rec}) + "\n")
                        sm[vname] = self_metrics(
                            {f: [{"start": s["start"], "end": s["end"]} for s in ivs]
                             for f, ivs in spans.items()},
                            row["gold_context"])
                    dt = time.perf_counter() - t0
                    summary.append({"instance_id": iid, "repo": local_repo,
                                    "n_seeds": len(seeds), "n_retrieved": len(retrieved),
                                    "ctx_tokens": ctx_tokens, "sec": round(dt, 2),
                                    "metrics": sm})
                    print(f"[{local_repo}] {iid} seeds={len(seeds)} retr={len(retrieved)} "
                          f"gap={len(retrieved_gap)} {dt:.1f}s  "
                          f"f1(s+r)={sm['seeds_plus_retrieved']['line_f1']} "
                          f"f1(gap)={sm['diffcontext_gap']['line_f1']}")
                    count += 1
                except Exception as e:
                    print(f"[{local_repo}] {iid} ERROR {type(e).__name__}: {e}")
                    summary.append({"instance_id": iid, "repo": local_repo, "error": str(e)[:200]})
    finally:
        for f in pred_files.values():
            f.close()
        if wt is not None:
            wt.remove()
    with open(os.path.join(args.out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    # Pooled quick report.
    n_ok = [s for s in summary if "error" not in s]
    if n_ok:
        import statistics as st
        for v in ("seeds_only", "retrieved_only", "seeds_plus_retrieved",
                  "diffcontext_gap"):
            r = [s["metrics"][v]["line_recall"] for s in n_ok]
            p = [s["metrics"][v]["line_precision"] for s in n_ok]
            f1 = [s["metrics"][v]["line_f1"] for s in n_ok]
            print(f"\nPOOLED {v} (n={len(n_ok)}): "
                  f"line_recall={st.mean(r):.3f} line_precision={st.mean(p):.3f} "
                  f"line_f1={st.mean(f1):.3f}")
    print(f"\nwrote {len(summary)} rows; pred files in {args.out_dir}")


if __name__ == "__main__":
    main()
