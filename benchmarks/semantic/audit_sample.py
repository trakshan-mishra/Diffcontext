#!/usr/bin/env python3
"""
audit_sample.py — stratified manual-audit sample over the mined pairs.

The mined ground truth is a PROXY: "changed in the same commit" is not the same
as "genuinely related". This pulls a representative, stratified sample of
(query, gt-symbol) relevance links for you to hand-label, so audit_stats.py can
measure how often the proxy is right (proxy precision) and WHERE it is
trustworthy — the validity question the whole semantic eval rests on.

Unit = a single (query, gt) relevance LINK, i.e. one quick
related/incidental/unsure judgment, NOT a whole pair. Link-level labels give
per-stratum proxy precision and a clean inter-rater kappa. Only links whose BOTH
endpoints are alive at HEAD (actually scorable in the eval corpus) are sampled.

Strata = repo x cross-file (query and gt in different files) x gt-set-size
bucket (sm 1-2 / md 3-5 / lg 6+). Cross-file and large-gt commits are exactly
where co-change is most likely incidental, so stratifying keeps them visible in
the estimate instead of drowned out by trivial same-file method clusters.

Allocation is proportional to stratum population (largest-remainder rounding),
so the OVERALL proxy-precision estimate is unbiased for the dataset. Rare strata
may get few links; bump --n or run a targeted second sample to tighten those.

Usage:
  python -m benchmarks.semantic.audit_sample --n 180 --seed 0
  python -m benchmarks.semantic.audit_sample --repos flask,httpx --n 100
Output:
  benchmarks/semantic/audit/sample.csv        <- fill the `label` column by hand
  benchmarks/semantic/audit/sample.meta.json
"""

import argparse
import csv
import glob
import json
import os
import random
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diffcontext.pipeline import index_repository

_HERE = os.path.dirname(os.path.abspath(__file__))
PAIRS_DIR = os.path.join(_HERE, "pairs")
AUDIT_DIR = os.path.join(_HERE, "audit")
MAX_CODE_LINES = 30       # clip long function bodies so the CSV stays readable

CSV_FIELDS = ["link_id", "repo", "commit", "cross_file", "gt_size", "gt_bucket",
              "commit_msg", "query_symbol", "gt_symbol", "query_code", "gt_code",
              "label", "notes"]


@dataclass
class Link:
    link_id: str
    repo: str
    commit: str
    cross_file: bool
    gt_size: int
    gt_bucket: str
    commit_msg: str
    query_symbol: str
    gt_symbol: str
    query_code: str
    gt_code: str


def gt_bucket(n: int) -> str:
    return "sm" if n <= 2 else "md" if n <= 5 else "lg"


def _file_of(sym_id: str) -> str:
    return sym_id.split(":", 1)[0]


def _clip(code: str, n: int = MAX_CODE_LINES) -> str:
    lines = code.splitlines()
    if len(lines) <= n:
        return code
    return "\n".join(lines[:n]) + f"\n... (+{len(lines) - n} more lines)"


def load_pairs(paths: List[str]) -> List[dict]:
    pairs: List[dict] = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            pairs.extend(json.loads(ln) for ln in f if ln.strip())
    return pairs


def build_links(pairs: List[dict], code_of: Callable[[str, str], Optional[str]]) -> List[Link]:
    """Flatten pairs into (query, gt) links, keeping only links whose BOTH
    endpoints resolve to HEAD source via `code_of(repo, sym_id) -> code|None`.
    Injecting the resolver keeps this pure and unit-testable without a repo."""
    links: List[Link] = []
    for pr in pairs:
        repo, q = pr["repo"], pr["query_symbol"]
        qcode = code_of(repo, q)
        if qcode is None:                     # query not alive at HEAD -> unscorable
            continue
        gts = pr["gt_symbols"]
        for g in gts:
            gcode = code_of(repo, g)
            if gcode is None:                 # gt not alive -> skip this link
                continue
            links.append(Link(
                link_id=f"{repo}:{pr['commit'][:10]}:{q}=>{g}",
                repo=repo, commit=pr["commit"],
                cross_file=_file_of(q) != _file_of(g),
                gt_size=len(gts), gt_bucket=gt_bucket(len(gts)),
                commit_msg=pr["commit_msg"], query_symbol=q, gt_symbol=g,
                query_code=_clip(qcode), gt_code=_clip(gcode)))
    return links


def _query_key(link: Link) -> tuple:
    return (link.repo, link.commit, link.query_symbol)


def query_stratum(link: Link) -> tuple:
    return (link.repo, link.gt_bucket)


def _allocate(sizes: Dict[tuple, int], n: int) -> Dict[tuple, int]:
    """Largest-remainder proportional allocation of n across strata, each
    allocation capped at the stratum's population; round-robin top-up covers any
    shortfall from strata too small to absorb their share."""
    total = sum(sizes.values())
    if total <= n:
        return dict(sizes)
    raw = {k: n * v / total for k, v in sizes.items()}
    alloc = {k: min(int(x), sizes[k]) for k, x in raw.items()}
    rem = n - sum(alloc.values())
    for k in sorted(sizes, key=lambda k: raw[k] - int(raw[k]), reverse=True):
        if rem <= 0:
            break
        if alloc[k] < sizes[k]:
            alloc[k] += 1
            rem -= 1
    pool = [k for k in sizes if alloc[k] < sizes[k]]
    i = 0
    while rem > 0 and pool:
        k = pool[i % len(pool)]
        alloc[k] += 1
        rem -= 1
        if alloc[k] >= sizes[k]:
            pool.remove(k)
            i -= 1
        i += 1
    return alloc


def stratified_sample(links: List[Link], n: int, seed: int = 0,
                      links_per_query: int = 3) -> List[Link]:
    """Query-capped stratified sample of up to `n` links.

    Query-instances are stratified by (repo, gt_bucket) and sampled
    proportionally; from each chosen query we take up to `links_per_query`
    links. This keeps every query roughly equal-weight so no single big commit
    dominates (a k-symbol commit otherwise contributes ~k links). cross-file is
    reported downstream, not stratified on, since it is a per-link property."""
    rng = random.Random(seed)
    if len(links) <= n:
        out = list(links)
        rng.shuffle(out)
        return out

    groups: Dict[tuple, List[Link]] = defaultdict(list)
    for lk in links:
        groups[_query_key(lk)].append(lk)
    by_stratum: Dict[tuple, List[tuple]] = defaultdict(list)
    for qk, ls in groups.items():
        by_stratum[query_stratum(ls[0])].append(qk)

    # Allocate the LINK budget across strata proportional to each stratum's
    # capped-available links (<=links_per_query per query), so the target is hit
    # regardless of how many links each query happens to have.
    capped = {s: sum(min(len(groups[qk]), links_per_query) for qk in qks)
              for s, qks in by_stratum.items()}
    alloc = _allocate(capped, n)

    out: List[Link] = []
    for s, qks in by_stratum.items():
        rng.shuffle(qks)
        got, target = 0, alloc[s]
        for qk in qks:
            if got >= target:
                break
            take = list(groups[qk])
            rng.shuffle(take)
            take = take[:min(links_per_query, target - got)]
            out.extend(take)
            got += len(take)
    rng.shuffle(out)
    return out[:n]


def _repo_paths(pairs_dir: str) -> Dict[str, str]:
    """repo-name -> checkout path, from the mining manifest (fallback: guess)."""
    man = os.path.join(pairs_dir, "manifest.json")
    paths: Dict[str, str] = {}
    if os.path.exists(man):
        with open(man, encoding="utf-8") as f:
            for r in json.load(f).get("repos", []):
                paths[r["repo"]] = r["path"]
    return paths


def cli_resolver(repo_paths: Dict[str, str]) -> Callable[[str, str], Optional[str]]:
    """Resolve HEAD source per symbol, indexing each repo at most once."""
    idx_cache: Dict[str, Dict] = {}

    def code_of(repo: str, sym_id: str) -> Optional[str]:
        if repo not in idx_cache:
            path = repo_paths.get(repo, os.path.join("benchmark_repos", repo))
            try:
                idx_cache[repo] = index_repository(path).symbols
            except Exception:            # noqa: BLE001 — unresolved repo -> no code
                idx_cache[repo] = {}
        sym = idx_cache[repo].get(sym_id)
        return sym.code if sym else None

    return code_of


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=180, help="links to sample (default %(default)s)")
    ap.add_argument("--links-per-query", type=int, default=3,
                    help="max links taken per query, to keep queries equal-weight "
                         "(default %(default)s)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--repos", default=None,
                    help="comma-separated repo names to restrict to (default: all mined)")
    ap.add_argument("--pairs-dir", default=PAIRS_DIR)
    ap.add_argument("--out-dir", default=AUDIT_DIR)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.pairs_dir, "*.jsonl")))
    if args.repos:
        keep = set(args.repos.split(","))
        files = [f for f in files if os.path.basename(f)[:-len(".jsonl")] in keep]
    if not files:
        sys.exit(f"no pairs .jsonl found in {args.pairs_dir} (run mine_pairs.py first)")

    pairs = load_pairs(files)
    resolver = cli_resolver(_repo_paths(args.pairs_dir))
    print(f"resolving HEAD source for {len(pairs)} pairs across {len(files)} repo(s) ...")
    links = build_links(pairs, resolver)
    print(f"{len(links)} scorable links (both endpoints alive@HEAD)")
    sample = stratified_sample(links, args.n, args.seed, args.links_per_query)

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, "sample.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for lk in sample:
            row = asdict(lk)
            row["label"] = ""     # you fill: related | incidental | unsure
            row["notes"] = ""
            w.writerow(row)

    strata_samp: Dict[str, int] = defaultdict(int)
    for lk in sample:
        strata_samp["/".join(map(str, query_stratum(lk)))] += 1
    n_queries = len({_query_key(lk) for lk in sample})
    n_cross = sum(1 for lk in sample if lk.cross_file)
    meta = {"seed": args.seed, "n_requested": args.n, "n_sampled": len(sample),
            "links_per_query": args.links_per_query, "n_distinct_queries": n_queries,
            "n_cross_file": n_cross, "n_links_total": len(links),
            "source_files": [os.path.basename(f) for f in files],
            "strata_sampled_by_repo_gtbucket": dict(sorted(strata_samp.items()))}
    with open(os.path.join(args.out_dir, "sample.meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"\n{len(sample)} links -> {os.path.relpath(csv_path)}")
    print("label each row `related` (retriever SHOULD surface it), `incidental` "
          "(co-changed but unrelated), or `unsure`, then run audit_stats.py")


if __name__ == "__main__":
    main()
