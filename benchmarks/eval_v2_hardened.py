#!/usr/bin/env python3
"""
eval_v2_hardened.py — Hardened co-change benchmark for DiffContext.

Addresses the known weaknesses of the original benchmark:

  W1  Sample-size inflation  -> mines DISTINCT COMMITS (target 50-100 per
      repo); reports BOTH per-commit aggregates (a commit counts once) and
      the per-symbol breakdown (original format). Stratifies by ground-truth
      set size (1-2 / 3-5 / 6+).
  W2  No baselines           -> BM25, same-file co-location, and Random-k
      where k is matched per-query to DiffContext's retrieval count.
  W3  Single retrieval budget -> precision/recall sweep at top-10/20/30/50/70
      for every method.
  W4  Anecdotal failure modes -> see failure_buckets() (Django): criteria-
      mined pair sets for (a) thematic-no-edge, (b) backend/dispatch
      override, (c) cross-subsystem; per-bucket hit rates.
  W5  Incomplete cross-repo table -> same methodology on every repo under
      benchmark_repos/.

Metric definitions reused from the original benchmark / eval_v1:
  hit        = 1 if any ground-truth symbol appears anywhere in the
               (budget-truncated) retrieved list
  precision  = |retrieved & GT| / |retrieved|
  recall     = |retrieved & GT| / |GT|
  f1         = harmonic mean
  p@k / r@k  = same, on the top-k prefix of the ranked list

Deviations from the original benchmark (explicit, per requirements):
  * TOKEN_BUDGET stays at the frozen eval_v1 value of 10,000 estimated
    tokens (the product default is 8,000); kept so numbers remain
    comparable with the frozen eval_v1 baseline.
  * The BM25 baseline indexes FULL function source (same as eval_v1),
    which is a STRONGER baseline than the name+docstring variant the spec
    suggests. If DiffContext loses to it, that is reported as a loss.
  * Random-k is deterministic (seeded per query) for reproducibility.

Usage:
  python benchmarks/eval_v2_hardened.py                        # all repos
  python benchmarks/eval_v2_hardened.py benchmark_repos/django # one repo
  python benchmarks/eval_v2_hardened.py --buckets              # Django failure buckets only
"""

import csv
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import build_reverse_graph

from benchmarks.ground_truth import _get_changed_line_ranges, _find_functions_at_lines
from benchmarks.baselines import BM25Baseline, FileCoLocationBaseline, RandomBaseline, _tokenize
from benchmarks.eval_v1 import (
    _graph_ranked, _graph_scores, _normalize,
    truncate_by_token_budget, bootstrap_ci,
    TOKEN_BUDGET, CANDIDATE_LIMIT,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TARGET_COMMITS   = 100          # distinct commits per repo (may find fewer)
SCAN_LIMIT       = 6000         # how many commits of history to scan
BUDGETS          = [10, 20, 30, 50, 70]
METHODS          = ["diffcontext", "hybrid", "bm25", "samefile", "random_k"]
NOISY_SYMBOLS    = 20           # >= this many changed symbols -> flag commit
NOISY_FILES      = 10           # >= this many changed .py files -> flag commit
RESULTS_DIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "eval_v2")

STRATA = [("1-2", 1, 2), ("3-5", 3, 5), ("6+", 6, 10**9)]


# ---------------------------------------------------------------------------
# Distinct-commit ground truth mining
# ---------------------------------------------------------------------------

@dataclass
class CommitCase:
    commit_hash: str
    commit_msg: str
    py_files: List[str]
    symbols: List[str]              # all changed function IDs at commit time
    flagged_noisy: bool = False
    flag_reason: str = ""


def extract_distinct_commits(
    repo_path: str,
    target: int = TARGET_COMMITS,
    scan_limit: int = SCAN_LIMIT,
) -> List[CommitCase]:
    """Mine up to `target` DISTINCT commits with >=2 co-changed functions."""
    repo_path = os.path.abspath(repo_path)
    try:
        log = subprocess.run(
            ["git", "log", f"--max-count={scan_limit}", "--format=%H|%s",
             "--no-merges", "--diff-filter=M"],
            cwd=repo_path, capture_output=True, text=True, timeout=60,
        )
        if log.returncode != 0:
            return []
        raw = [ln.split("|", 1) for ln in log.stdout.strip().split("\n") if "|" in ln]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    out: List[CommitCase] = []
    for commit_hash, msg in raw:
        if len(out) >= target:
            break
        try:
            files_res = subprocess.run(
                ["git", "diff", "--name-only", "--relative", "--diff-filter=M",
                 f"{commit_hash}~1", commit_hash],
                cwd=repo_path, capture_output=True, text=True, timeout=10,
            )
            if files_res.returncode != 0:
                continue
        except subprocess.TimeoutExpired:
            continue

        py_files = [
            f for f in files_res.stdout.strip().split("\n")
            if f.endswith(".py")
            and "/test" not in f.lower()
            and "/tests/" not in f.lower()
            and "test_" not in os.path.basename(f)
            and f.strip()
        ]
        if not py_files:
            continue

        changed: List[str] = []
        for fp in py_files:
            lines = _get_changed_line_ranges(repo_path, commit_hash, fp)
            if not lines:
                continue
            changed.extend(_find_functions_at_lines(fp, lines, repo_path, commit_hash))
        changed = list(dict.fromkeys(changed))
        if len(changed) < 2:
            continue

        noisy, reason = False, ""
        if len(changed) >= NOISY_SYMBOLS:
            noisy, reason = True, f"{len(changed)} symbols changed (likely mechanical refactor)"
        elif len(py_files) >= NOISY_FILES:
            noisy, reason = True, f"{len(py_files)} files changed (likely sweeping change)"

        out.append(CommitCase(
            commit_hash=commit_hash[:10], commit_msg=msg[:100],
            py_files=py_files, symbols=changed,
            flagged_noisy=noisy, flag_reason=reason,
        ))
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def case_metrics(ranked: List[str], gt: Set[str]) -> Dict[str, float]:
    """hit / P / R / F1 on the full budgeted list, plus P@k,R@k for BUDGETS."""
    rset = set(ranked)
    tp = len(rset & gt)
    prec = tp / len(rset) if rset else 0.0
    rec = tp / len(gt) if gt else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    m = {
        "hit": 1.0 if tp > 0 else 0.0,
        "precision": prec, "recall": rec, "f1": f1,
        "retrieved_n": float(len(ranked)),
    }
    for k in BUDGETS:
        top = set(ranked[:k])
        tpk = len(top & gt)
        m[f"p@{k}"] = tpk / k
        m[f"r@{k}"] = tpk / len(gt) if gt else 0.0
    return m


METRIC_KEYS = (["hit", "precision", "recall", "f1", "retrieved_n"]
               + [f"p@{k}" for k in BUDGETS] + [f"r@{k}" for k in BUDGETS])


def mean_metrics(rows: List[Dict[str, float]]) -> Dict[str, float]:
    if not rows:
        return {k: 0.0 for k in METRIC_KEYS}
    return {k: sum(r[k] for r in rows) / len(rows) for k in METRIC_KEYS}


# ---------------------------------------------------------------------------
# Per-repo evaluation
# ---------------------------------------------------------------------------

def _hybrid_ranked(q, symbols, symbol_ids, graph, reverse_graph, bm25) -> List[str]:
    """graph+bm25+file blend (0.5/0.35/0.15), same recipe as eval_v1's winner."""
    g_scores = _normalize(_graph_scores(q, graph, reverse_graph))
    q_tokens = _tokenize(symbols[q].code)
    bm25_raw = bm25.bm25.get_scores(q_tokens)
    b_scores = _normalize({sid: bm25_raw[i] for i, sid in enumerate(symbol_ids)
                           if sid != q and bm25_raw[i] > 0})
    q_file = q.split(":")[0] if ":" in q else ""
    combined: Dict[str, float] = defaultdict(float)
    for sid, sc in g_scores.items():
        combined[sid] += 0.5 * sc
    for sid, sc in b_scores.items():
        combined[sid] += 0.35 * sc
    for sid in symbol_ids:
        if sid != q and sid.split(":")[0] == q_file:
            combined[sid] += 0.15
    ranked = [s for s, _ in sorted(combined.items(), key=lambda x: x[1], reverse=True)
              if s != q][:CANDIDATE_LIMIT]
    return truncate_by_token_budget(symbols, ranked)


def evaluate_repo(repo_path: str) -> Optional[Dict]:
    repo_name = os.path.basename(os.path.abspath(repo_path))
    print(f"\n{'='*70}\n  {repo_name}\n{'='*70}")

    t0 = time.perf_counter()
    commits = extract_distinct_commits(repo_path)
    print(f"  Distinct commits mined: {len(commits)} "
          f"(flagged noisy: {sum(c.flagged_noisy for c in commits)}) "
          f"in {time.perf_counter()-t0:.0f}s")
    if not commits:
        return None

    symbols = extract_all_symbols(repo_path)
    graph = build_repository_graph(repo_path)
    reverse_graph = build_reverse_graph(graph)
    sset = set(symbols.keys())
    symbol_ids = list(symbols.keys())
    print(f"  Symbols: {len(symbols)}  Edges: {sum(len(v) for v in graph.values())}")

    bm25 = BM25Baseline(symbols)
    file_bl = FileCoLocationBaseline(symbols)
    rand_bl = RandomBaseline(symbols)

    # keep only commits with >=2 changed symbols still present at HEAD
    valid_commits = []
    for c in commits:
        vs = [s for s in c.symbols if s in sset]
        if len(vs) >= 2:
            valid_commits.append((c, vs))
    print(f"  Valid commits (>=2 symbols alive at HEAD): {len(valid_commits)}")
    if not valid_commits:
        return None

    rows: List[Dict] = []          # per (commit, query, method) case rows
    t0 = time.perf_counter()
    for c, vsyms in valid_commits:
        for q in vsyms:
            gt = set(vsyms) - {q}
            g_ranked = truncate_by_token_budget(
                symbols, _graph_ranked(q, graph, reverse_graph))
            k_match = max(len(g_ranked), 1)
            method_ranked = {
                "diffcontext": g_ranked,
                "hybrid": _hybrid_ranked(q, symbols, symbol_ids, graph, reverse_graph, bm25),
                "bm25": truncate_by_token_budget(symbols, bm25.retrieve(q, top_k=CANDIDATE_LIMIT)),
                "samefile": truncate_by_token_budget(symbols, file_bl.retrieve(q, top_k=CANDIDATE_LIMIT)),
                "random_k": rand_bl.retrieve(q, top_k=k_match),
            }
            for method, ranked in method_ranked.items():
                m = case_metrics(ranked, gt)
                rows.append({
                    "repo": repo_name,
                    "commit": c.commit_hash,
                    "commit_msg": c.commit_msg,
                    "flagged_noisy": int(c.flagged_noisy),
                    "flag_reason": c.flag_reason,
                    "query": q,
                    "gt_size": len(gt),
                    "method": method,
                    **{k: round(v, 4) for k, v in m.items()},
                })
    print(f"  Evaluated {len(rows)//len(METHODS)} symbol-queries x {len(METHODS)} methods "
          f"in {time.perf_counter()-t0:.0f}s")

    # ── Aggregations ──────────────────────────────────────────────────────
    summary: Dict = {
        "repo": repo_name,
        "n_commits": len(valid_commits),
        "n_commits_flagged_noisy": sum(c.flagged_noisy for c, _ in valid_commits),
        "n_symbol_queries": len(rows) // len(METHODS),
        "config": {
            "token_budget": TOKEN_BUDGET, "candidate_limit": CANDIDATE_LIMIT,
            "budgets": BUDGETS, "target_commits": TARGET_COMMITS,
            "scan_limit": SCAN_LIMIT, "seed": 42,
            "noisy_flag": f">={NOISY_SYMBOLS} symbols or >={NOISY_FILES} files",
        },
        "methods": {},
        "flagged_commits": [
            {"commit": c.commit_hash, "msg": c.commit_msg, "reason": c.flag_reason,
             "n_symbols": len(vs)}
            for c, vs in valid_commits if c.flagged_noisy
        ],
    }

    for method in METHODS:
        mrows = [r for r in rows if r["method"] == method]
        # per-symbol aggregate (original format)
        per_symbol = mean_metrics(mrows)
        # per-commit aggregate: mean within commit, then mean across commits
        by_commit: Dict[str, List[Dict]] = defaultdict(list)
        for r in mrows:
            by_commit[r["commit"]].append(r)
        commit_means = [mean_metrics(v) for v in by_commit.values()]
        per_commit = mean_metrics(commit_means)
        ci = {k: list(bootstrap_ci([cm[k] for cm in commit_means]))
              for k in ("hit", "recall", "precision", "f1", "r@20")}
        # per-commit aggregate excluding flagged-noisy commits
        clean_means = [mean_metrics(v) for cid, v in by_commit.items()
                       if not v[0]["flagged_noisy"]]
        per_commit_clean = mean_metrics(clean_means)
        # strata by GT size (per-symbol level)
        strata = {}
        for label, lo, hi in STRATA:
            srows = [r for r in mrows if lo <= r["gt_size"] <= hi]
            strata[label] = {"n": len(srows), **{k: round(v, 4) for k, v in mean_metrics(srows).items()}}

        summary["methods"][method] = {
            "per_symbol": {"n": len(mrows), **{k: round(v, 4) for k, v in per_symbol.items()}},
            "per_commit": {"n": len(commit_means), **{k: round(v, 4) for k, v in per_commit.items()}},
            "per_commit_ci95": ci,
            "per_commit_excl_noisy": {"n": len(clean_means),
                                      **{k: round(v, 4) for k, v in per_commit_clean.items()}},
            "strata_by_gt_size": strata,
        }

    # ── Console tables ────────────────────────────────────────────────────
    print(f"\n  Per-COMMIT aggregates (n={summary['methods']['diffcontext']['per_commit']['n']} commits; a commit counts once)")
    hdr = f"  {'method':<13}{'hit':>7}{'prec':>8}{'rec':>8}{'f1':>8}" + "".join(f"{'r@'+str(k):>8}" for k in BUDGETS)
    print(hdr + "\n  " + "-" * (len(hdr) - 2))
    for method in METHODS:
        pc = summary["methods"][method]["per_commit"]
        print(f"  {method:<13}{pc['hit']:>7.3f}{pc['precision']:>8.3f}{pc['recall']:>8.3f}{pc['f1']:>8.3f}"
              + "".join(f"{pc['r@'+str(k)]:>8.3f}" for k in BUDGETS))

    print(f"\n  Budget sweep, per-commit precision/recall (diffcontext)")
    dc = summary["methods"]["diffcontext"]["per_commit"]
    for k in BUDGETS:
        print(f"    top-{k:<3}  P={dc[f'p@{k}']:.3f}  R={dc[f'r@{k}']:.3f}")

    return {"summary": summary, "rows": rows}


# ---------------------------------------------------------------------------
# Deliverable 4: targeted failure-mode buckets (Django)
# ---------------------------------------------------------------------------

def _undirected_within(graph, reverse_graph, src: str, max_hops: int, cap: int = 30000) -> Set[str]:
    """Symbols reachable from src within max_hops treating edges as undirected."""
    seen = {src}
    frontier = [src]
    for _ in range(max_hops):
        nxt = []
        for nd in frontier:
            for nb in list(graph.get(nd, [])) + list(reverse_graph.get(nd, set())):
                if nb not in seen:
                    seen.add(nb)
                    nxt.append(nb)
                    if len(seen) > cap:
                        return seen
        frontier = nxt
        if not frontier:
            break
    return seen


def _subsystem(sid: str) -> str:
    """Top-level Django subsystem of a symbol id, e.g. './django/db/...' -> 'db'."""
    path = sid.split(":")[0].lstrip("./")
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "django":
        if parts[1] == "contrib" and len(parts) >= 3:
            return f"contrib/{parts[2]}"
        return parts[1]
    return parts[0]


def failure_buckets(repo_path: str, per_bucket: int = 20) -> Dict:
    """
    Build the three targeted failure-mode pair sets from Django history and
    measure per-bucket hit rates for diffcontext / bm25 / hybrid.

    A "pair" is (query_symbol, target_symbol) co-changed in one commit.
    Selection is criteria-driven over mined commits (documented per bucket),
    with the selected pairs + commit messages emitted for manual audit.
    """
    repo_name = os.path.basename(os.path.abspath(repo_path))
    print(f"\n{'='*70}\n  FAILURE-MODE BUCKETS: {repo_name}\n{'='*70}")

    commits = extract_distinct_commits(repo_path, target=250, scan_limit=8000)
    symbols = extract_all_symbols(repo_path)
    graph = build_repository_graph(repo_path)
    reverse_graph = build_reverse_graph(graph)
    sset = set(symbols.keys())
    symbol_ids = list(symbols.keys())
    bm25 = BM25Baseline(symbols)

    def has_edge(a, b):
        return b in graph.get(a, []) or a in graph.get(b, [])

    buckets: Dict[str, List[Dict]] = {"thematic_no_edge": [], "backend_dispatch": [],
                                      "cross_subsystem": []}
    seen_pairs: Set[Tuple[str, str]] = set()

    for c in commits:
        vs = [s for s in c.symbols if s in sset]
        if len(vs) < 2 or c.flagged_noisy:
            continue
        for q in vs:
            for g in vs:
                if q == g or (q, g) in seen_pairs:
                    continue
                q_file, g_file = q.split(":")[0], g.split(":")[0]
                if q_file == g_file or has_edge(q, g):
                    continue                      # trivially reachable pairs excluded
                q_name = q.split(":")[1].split(".")[-1]
                g_name = g.split(":")[1].split(".")[-1]
                q_sub, g_sub = _subsystem(q), _subsystem(g)
                pair = {
                    "commit": c.commit_hash, "msg": c.commit_msg,
                    "query": q, "target": g,
                }

                # Bucket B: backend/dispatch override — same method name in
                # different files (classic override), or pair touching
                # db/backends/ vendor dirs.
                is_backend = ("/db/backends/" in q_file or "/db/backends/" in g_file)
                if (q_name == g_name and q_name != "__init__") or is_backend:
                    if len(buckets["backend_dispatch"]) < per_bucket:
                        buckets["backend_dispatch"].append(pair)
                        seen_pairs.add((q, g))
                    continue

                # need hop distance for the remaining two buckets
                near = _undirected_within(graph, reverse_graph, q, max_hops=3)
                if g in near:
                    continue                      # reachable within 3 hops -> not a structural gap

                if q_sub == g_sub:
                    # Bucket A: thematic — same subsystem, no path within 3 hops
                    if len(buckets["thematic_no_edge"]) < per_bucket:
                        buckets["thematic_no_edge"].append(pair)
                        seen_pairs.add((q, g))
                else:
                    # Bucket C: cross-subsystem conceptual link
                    if len(buckets["cross_subsystem"]) < per_bucket:
                        buckets["cross_subsystem"].append(pair)
                        seen_pairs.add((q, g))
        if all(len(v) >= per_bucket for v in buckets.values()):
            break

    # ── Evaluate: does each method retrieve `target` given `query`? ────────
    results: Dict = {"repo": repo_name, "per_bucket": {}}
    for bname, pairs in buckets.items():
        hits = {"diffcontext": 0, "bm25": 0, "hybrid": 0}
        detailed = []
        for p in pairs:
            q, g = p["query"], p["target"]
            g_ranked = truncate_by_token_budget(symbols, _graph_ranked(q, graph, reverse_graph))
            b_ranked = truncate_by_token_budget(symbols, bm25.retrieve(q, top_k=CANDIDATE_LIMIT))
            h_ranked = _hybrid_ranked(q, symbols, symbol_ids, graph, reverse_graph, bm25)
            row_hits = {
                "diffcontext": int(g in set(g_ranked)),
                "bm25": int(g in set(b_ranked)),
                "hybrid": int(g in set(h_ranked)),
            }
            for m in hits:
                hits[m] += row_hits[m]
            detailed.append({**p, **{f"hit_{m}": v for m, v in row_hits.items()}})
        n = len(pairs)
        results["per_bucket"][bname] = {
            "n_pairs": n,
            "hit_rate": {m: round(h / n, 3) if n else 0.0 for m, h in hits.items()},
            "pairs": detailed,
        }
        print(f"\n  {bname}  (n={n} pairs)")
        for m, h in hits.items():
            print(f"    {m:<13} hit rate: {h}/{n} = {h/n:.1%}" if n else "    (empty)")

    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_outputs(all_repo_results: List[Dict], bucket_results: Optional[Dict]):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    all_summaries = []
    for rr in all_repo_results:
        name = rr["summary"]["repo"]
        csv_path = os.path.join(RESULTS_DIR, f"{name}_cases.csv")
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rr["rows"][0].keys()))
            w.writeheader()
            w.writerows(rr["rows"])
        with open(os.path.join(RESULTS_DIR, f"{name}_summary.json"), "w") as f:
            json.dump(rr["summary"], f, indent=2)
        all_summaries.append(rr["summary"])
        print(f"  saved {csv_path} ({len(rr['rows'])} rows)")
    with open(os.path.join(RESULTS_DIR, "all_summaries.json"), "w") as f:
        json.dump(all_summaries, f, indent=2)
    if bucket_results:
        with open(os.path.join(RESULTS_DIR, "failure_buckets_django.json"), "w") as f:
            json.dump(bucket_results, f, indent=2)
        print(f"  saved failure_buckets_django.json")


def main():
    args = [a for a in sys.argv[1:]]
    buckets_only = "--buckets" in args
    repo_args = [a for a in args if os.path.isdir(a)]

    bench_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "benchmark_repos"))
    if repo_args:
        repo_paths = repo_args
    else:
        repo_paths = sorted(
            os.path.join(bench_dir, n) for n in os.listdir(bench_dir)
            if os.path.isdir(os.path.join(bench_dir, n, ".git"))
        )

    bucket_results = None
    all_results = []
    if not buckets_only:
        for rp in repo_paths:
            r = evaluate_repo(rp)
            if r:
                all_results.append(r)

    django_path = os.path.join(bench_dir, "django")
    if os.path.isdir(django_path) and (buckets_only or not repo_args or any("django" in rp for rp in repo_paths)):
        bucket_results = failure_buckets(django_path)

    save_outputs(all_results, bucket_results)
    print("\nDone.")


if __name__ == "__main__":
    main()
