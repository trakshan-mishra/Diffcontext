#!/usr/bin/env python3
"""
mine_pairs.py — build the semantic-retrieval eval dataset from git co-change.

This is the persisted, versioned (query, context) corpus that the whole
CodeXEmbed vs structural vs hybrid comparison (Items 2-6) consumes. It reuses
the SAME commit miner your structural eval already trusts —
benchmarks/eval_v2_hardened.extract_distinct_commits — so the mined set is
selected identically to retrieval_head2head / eval_v2 and the three-way
ablation is genuinely apples-to-apples. That miner already gives us the two
properties a naive per-symbol extractor does not:

  * distinct commits (breadth), NOT one case per changed symbol. A per-symbol
    extractor emits ~k^2 near-duplicate links for a k-symbol commit, so a
    handful of mega-refactors swamp everything (measured: httpx had 136/300
    pairs from ONE commit).
  * a noise flag: >=20 changed symbols or >=10 changed .py files == "mechanical
    refactor / sweeping change", excluded by default here exactly as the
    structural eval excludes it. Mass co-change is incidental, not a relevance
    signal, and it's the dominant source of bad ground-truth labels.

On top of the miner we add what the semantic track needs and the miner doesn't
emit: full commit SHA + commit timestamp (the temporal-split key — Item 5's
co-change/recency feature MUST be computed only from commits strictly older
than a pair's commit_ts, or GT and feature are circular and the win is fake),
alive-at-HEAD annotation (the retrieval corpus is the HEAD index), and a stable
JSONL + manifest schema.

Query = a changed symbol (code-query, symbol->symbol); ground truth = the other
symbols changed in the same commit. Up to --queries-per-commit queries are drawn
per commit (seeded by the commit hash, so reproducible and unbiased — not the
"first two symbols"), keeping every commit roughly equal-weight.

Usage:
  python -m benchmarks.semantic.mine_pairs benchmark_repos/click benchmark_repos/flask
  python -m benchmarks.semantic.mine_pairs benchmark_repos/*/ --target-commits 200

Output:
  benchmarks/semantic/pairs/<repo>.jsonl    one MinedPair per line
  benchmarks/semantic/pairs/manifest.json   params + per-repo provenance

Cost: pure git + AST + one HEAD index per repo. Seconds to ~10s/repo (the HEAD
index dominates on the largest repos). No embeddings here — that bottleneck
lands in Item 4, which is why every symbol is referenced by a stable id the
embedding cache can key on.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
import warnings
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from benchmarks.eval_v2_hardened import extract_distinct_commits
from diffcontext.pipeline import index_repository

DATASET_VERSION = 2         # v2: extract_distinct_commits + noise exclusion + query cap
PAIRS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pairs")
# Each scanned commit costs a git-diff + per-file git-show + ast.parse, so scan
# time is the bottleneck (~1 min per ~1500 commits scanned). 2500 finds ample
# clean commits on these repos; raise it only for a repo with sparse co-change.
DEFAULT_SCAN_LIMIT = 2500
DEFAULT_TARGET_COMMITS = 120
DEFAULT_QUERIES_PER_COMMIT = 2
DEFAULT_MIN_ALIVE = 2       # need query + >=1 co-changed symbol alive at HEAD


@dataclass
class MinedPair:
    repo: str
    commit: str                 # full 40-char SHA (provenance + git lookups)
    commit_ts: int              # unix commit time — temporal-split key (anti-contamination)
    commit_msg: str
    query_symbol: str           # code-query: THIS symbol's source is the query
    gt_symbols: List[str]       # other symbols changed in the same commit = labels
    n_changed_files: int
    query_alive_at_head: Optional[bool] = None   # scorable against the HEAD corpus?
    n_gt_alive_at_head: Optional[int] = None     # how many labels survived to HEAD


def _git(repo: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=timeout)


def _commit_meta(repo: str, short_sha: str,
                 cache: Dict[str, Optional[Tuple[str, int]]]) -> Optional[Tuple[str, int]]:
    """(full_sha, unix_ts) for a possibly-abbreviated SHA, memoized per repo.

    The miner returns 10-char short hashes; the dataset stores full SHAs
    (collision-proof provenance) and the commit time (temporal key)."""
    if short_sha in cache:
        return cache[short_sha]
    r = _git(repo, "show", "-s", "--format=%H|%ct", short_sha, timeout=10)
    meta: Optional[Tuple[str, int]] = None
    if r.returncode == 0 and "|" in r.stdout:
        full, ct = r.stdout.strip().split("|", 1)
        if ct.isdigit():
            meta = (full, int(ct))
    cache[short_sha] = meta
    return meta


def build_repo_pairs(repo: str, target_commits: int = DEFAULT_TARGET_COMMITS,
                     queries_per_commit: int = DEFAULT_QUERIES_PER_COMMIT,
                     min_alive: int = DEFAULT_MIN_ALIVE, keep_noisy: bool = False,
                     scan_limit: int = DEFAULT_SCAN_LIMIT) -> Tuple[List[MinedPair], dict]:
    """Mine one repo -> (pairs, provenance).

    Distinct commits from extract_distinct_commits (noise-flagged), HEAD index
    to keep only scorable symbols, then up to `queries_per_commit` queries per
    commit drawn reproducibly from that commit's alive symbols.
    """
    repo = os.path.abspath(repo)
    name = os.path.basename(repo.rstrip("/"))

    # Historical third-party sources (e.g. black's test fixtures) contain
    # invalid escape sequences; ast.parse emits SyntaxWarning but still parses.
    # Not ours to fix and zero signal — silence so the mining log stays readable.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        commits = extract_distinct_commits(repo, target=target_commits, scan_limit=scan_limit)

        # HEAD index defines the retrieval corpus. Best-effort: a repo that
        # fails to index still yields pairs, with alive flags left None and
        # every commit-time symbol treated as a candidate query.
        alive: Optional[Set[str]] = None
        n_symbols_head: Optional[int] = None
        try:
            idx = index_repository(repo)
            alive = set(idx.symbols)
            n_symbols_head = len(idx.symbols)
        except Exception as e:  # noqa: BLE001 — indexing is never fatal to mining
            print(f"  warn: HEAD index failed ({type(e).__name__}); alive flags omitted")

    meta_cache: Dict[str, Optional[Tuple[str, int]]] = {}
    pairs: List[MinedPair] = []
    n_noisy = 0
    for c in commits:
        if c.flagged_noisy and not keep_noisy:
            n_noisy += 1
            continue
        candidates = [s for s in c.symbols if s in alive] if alive is not None else list(c.symbols)
        if len(candidates) < min_alive:
            continue
        meta = _commit_meta(repo, c.commit_hash, meta_cache)
        if meta is None:
            continue
        full_sha, ts = meta
        # reproducible, order-independent query pick — seed on the commit hash
        rng = random.Random(c.commit_hash)
        queries = rng.sample(candidates, min(queries_per_commit, len(candidates)))
        for q in queries:
            gt = [s for s in c.symbols if s != q]        # full co-changed set = labels
            p = MinedPair(
                repo=name, commit=full_sha, commit_ts=ts, commit_msg=c.commit_msg,
                query_symbol=q, gt_symbols=gt, n_changed_files=len(c.py_files),
            )
            if alive is not None:
                p.query_alive_at_head = True             # q was drawn from `alive`
                p.n_gt_alive_at_head = sum(1 for g in gt if g in alive)
            pairs.append(p)

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    prov = {
        "repo": name, "path": repo, "head_sha": head, "n_symbols_head": n_symbols_head,
        "n_commits": len(commits), "n_noisy_excluded": n_noisy, "n_pairs": len(pairs),
        "n_distinct_commits_used": len({p.commit for p in pairs}),
    }
    return pairs, prov


def write_dataset(repos: List[str], out_dir: str, target_commits: int,
                  queries_per_commit: int, min_alive: int, keep_noisy: bool,
                  scan_limit: int) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    manifest: dict = {
        "dataset_version": DATASET_VERSION, "generated_ts": int(time.time()),
        "miner": "benchmarks.eval_v2_hardened.extract_distinct_commits",
        "params": {
            "target_commits": target_commits, "queries_per_commit": queries_per_commit,
            "min_alive": min_alive, "keep_noisy": keep_noisy, "scan_limit": scan_limit,
            "noisy_flag": ">=20 changed symbols or >=10 changed .py files",
            "query": "code-query symbol->symbol", "granularity": "symbol",
            "corpus": "repo index at HEAD",
        },
        "repos": [], "total_pairs": 0,
    }
    for r in repos:
        name = os.path.basename(os.path.abspath(r).rstrip("/"))
        print(f"mining {name} ...", flush=True)
        pairs, prov = build_repo_pairs(r, target_commits, queries_per_commit,
                                       min_alive, keep_noisy, scan_limit)
        out_path = os.path.join(out_dir, name + ".jsonl")
        with open(out_path, "w", encoding="utf-8") as f:
            for p in pairs:
                f.write(json.dumps(asdict(p)) + "\n")
        prov["out"] = os.path.relpath(out_path)
        manifest["repos"].append(prov)
        manifest["total_pairs"] += len(pairs)
        print(f"  {len(pairs)} pairs from {prov['n_distinct_commits_used']} commits"
              f" ({prov['n_noisy_excluded']} noisy commits excluded)"
              f" -> {os.path.relpath(out_path)}")

    man_path = os.path.join(out_dir, "manifest.json")
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n{manifest['total_pairs']} total pairs across {len(repos)} repo(s)"
          f" -> {os.path.relpath(man_path)}")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="+", help="paths to git repo clones to mine")
    ap.add_argument("--out-dir", default=PAIRS_DIR)
    ap.add_argument("--target-commits", type=int, default=DEFAULT_TARGET_COMMITS,
                    help="distinct commits to mine per repo (default %(default)s)")
    ap.add_argument("--queries-per-commit", type=int, default=DEFAULT_QUERIES_PER_COMMIT,
                    help="queries drawn per commit, keeping commits equal-weight "
                         "(default %(default)s)")
    ap.add_argument("--min-alive", type=int, default=DEFAULT_MIN_ALIVE,
                    help="min co-changed symbols alive at HEAD to use a commit "
                         "(query + >=1 label; default %(default)s)")
    ap.add_argument("--keep-noisy", action="store_true",
                    help="include mechanical-refactor commits (>=20 symbols or "
                         ">=10 files) instead of excluding them")
    ap.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT,
                    help="commits of history to scan per repo; scan time is the "
                         "bottleneck (default %(default)s)")
    args = ap.parse_args()
    write_dataset(args.repos, args.out_dir, args.target_commits,
                  args.queries_per_commit, args.min_alive, args.keep_noisy,
                  args.scan_limit)


if __name__ == "__main__":
    main()
