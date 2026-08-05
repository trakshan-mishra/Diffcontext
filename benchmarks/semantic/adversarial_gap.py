#!/usr/bin/env python3
"""
adversarial_gap.py — the "structural should win, embeddings should fail" test set.

The relatedness label here is a REAL code relationship, not co-change: a pair
(query, gt) qualifies only if the dependency graph has an actual edge between
them — query calls/references gt, or the reverse. That is a definitional
guarantee they are related (one literally uses the other), which co-change
alone (Item 1) cannot give — co-change can be incidental. Co-change is recorded
only as an optional annotation (`co_changed`) so the double-confirmed pairs are
visible; it is never the selector.

The ADVERSARIAL subset is the low-lexical-overlap tail of those real edges: the
two functions share little vocabulary (think `save_user` calling
`check_password` — genuinely related, zero shared words). A retriever that
works by text/semantic similarity is structurally blind to these; a
graph-following retriever catches them by design. This subset isolates exactly
that gap.

HONEST THREAT TO VALIDITY (read before quoting any number from Item 4 on this
set): DiffContext's structural retriever IS the graph, so it recovers these
edges nearly by construction — a structural "win" here is partly circular. The
genuinely non-circular quantity this set measures is how often SEMANTIC-ONLY
retrieval misses real, low-vocabulary code relationships; structural/hybrid
recovery is the complement. Report it as "embedding's blind spot", not "a fair
fight structural won".

Lexical overlap = Jaccard over domain tokens (identifiers split on snake_case /
camelCase, Python keywords and 1-2 char tokens dropped, so it reflects domain
vocabulary, not `self`/`return` boilerplate). The gap cut keeps the bottom
`--percentile` of edges by body-token Jaccard.

Two properties of this metric to keep in mind: (1) a caller's body contains the
callee's name (it calls it), so call edges rarely hit exactly 0 overlap — the
percentile cut adapts by taking the LEAST-similar real edges rather than an
absolute zero. (2) token-Jaccard is a CONSERVATIVE proxy for embedding
distance: a function dominated by one topic with a single cross-topic call reads
as more similar to tokens than to an embedding, so pairs flagged low-overlap
here are reliably hard for embeddings — the true gap Item 4 measures may be
wider, not narrower. `name_jaccard` is recorded alongside as the cleaner
"do the identifiers share words" signal.

Usage:
  python -m benchmarks.semantic.adversarial_gap benchmark_repos/click benchmark_repos/flask
  python -m benchmarks.semantic.adversarial_gap benchmark_repos/*/ --percentile 25

Output:
  benchmarks/semantic/gap/<repo>.jsonl    one GapPair per line
  benchmarks/semantic/gap/manifest.json   thresholds + distribution stats
"""

import argparse
import json
import keyword
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diffcontext.pipeline import index_repository

_HERE = os.path.dirname(os.path.abspath(__file__))
PAIRS_DIR = os.path.join(_HERE, "pairs")
GAP_DIR = os.path.join(_HERE, "gap")
DEFAULT_PERCENTILE = 25.0        # keep the bottom quartile by lexical overlap
DEFAULT_MIN_TOKENS = 3           # skip token-starved symbols (trivial 0-Jaccard)

_STOP = {w.lower() for w in keyword.kwlist} | {"self", "cls", "args", "kwargs"}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SUBWORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")   # split camelCase pieces


def tokenize_code(text: str) -> Set[str]:
    """Domain tokens: identifiers split on snake_case + camelCase, lowercased,
    keywords and <=2-char tokens dropped."""
    toks: Set[str] = set()
    for ident in _WORD.findall(text):
        for piece in ident.split("_"):
            for sub in _SUBWORD.findall(piece):
                w = sub.lower()
                if len(w) > 2 and w not in _STOP:
                    toks.add(w)
    return toks


def name_tokens(sym_id: str) -> Set[str]:
    """Domain tokens from the symbol NAME only (`./f.py:A.save_user` -> save,user)."""
    return tokenize_code(sym_id.split(":", 1)[1]) if ":" in sym_id else set()


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


@dataclass
class GapPair:
    repo: str
    query_symbol: str
    gt_symbol: str
    edge_dir: str            # "callee" (query -> gt) or "caller" (gt -> query)
    body_jaccard: float      # lexical overlap of the two function bodies
    name_jaccard: float      # lexical overlap of the two symbol names
    cross_file: bool
    co_changed: bool         # bonus: did they also change together (Item 1)?


def load_cochange(repo: str, pairs_dir: str) -> Set[FrozenSet[str]]:
    """Undirected {query, gt} pairs that co-changed, from the Item-1 dataset."""
    path = os.path.join(pairs_dir, repo + ".jsonl")
    out: Set[FrozenSet[str]] = set()
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if not ln.strip():
                continue
            p = json.loads(ln)
            for g in p["gt_symbols"]:
                out.add(frozenset((p["query_symbol"], g)))
    return out


def build_gap_set(index, repo: str, percentile: float = DEFAULT_PERCENTILE,
                  min_tokens: int = DEFAULT_MIN_TOKENS,
                  cochange: Optional[Set[FrozenSet[str]]] = None) -> Tuple[List[GapPair], dict]:
    """Real graph edges -> low-lexical-overlap tail = the adversarial gap set."""
    syms = index.symbols
    toks = {sid: tokenize_code(s.code) for sid, s in syms.items()}
    rev = index.reverse_graph

    # one directed (query, gt) per real edge in either direction; dedupe on the
    # ordered pair so a mutual call isn't double-counted.
    jac: Dict[Tuple[str, str], float] = {}
    for q in syms:
        related = set(index.graph.get(q, ())) | set(rev.get(q, ()))
        for g in related:
            if g == q or g not in syms or (q, g) in jac:
                continue
            if len(toks[q]) < min_tokens or len(toks[g]) < min_tokens:
                continue
            jac[(q, g)] = jaccard(toks[q], toks[g])

    if not jac:
        return [], {"repo": repo, "n_symbols": len(syms), "n_edge_pairs": 0, "n_gap": 0}

    vals = sorted(jac.values())
    thr = _percentile(vals, percentile)
    cochange = cochange or set()
    out: List[GapPair] = []
    for (q, g), bj in jac.items():
        if bj > thr:
            continue
        out.append(GapPair(
            repo=repo, query_symbol=q, gt_symbol=g,
            edge_dir="callee" if g in index.graph.get(q, ()) else "caller",
            body_jaccard=round(bj, 4),
            name_jaccard=round(jaccard(name_tokens(q), name_tokens(g)), 4),
            cross_file=q.split(":", 1)[0] != g.split(":", 1)[0],
            co_changed=frozenset((q, g)) in cochange,
        ))
    stats = {
        "repo": repo, "n_symbols": len(syms), "n_edge_pairs": len(jac), "n_gap": len(out),
        "jaccard_cut": round(thr, 4),
        "jaccard_p50": round(_percentile(vals, 50), 4),
        "jaccard_p90": round(_percentile(vals, 90), 4),
        "n_gap_cross_file": sum(1 for p in out if p.cross_file),
        "n_gap_co_changed": sum(1 for p in out if p.co_changed),
    }
    return out, stats


def write_gap(repos: List[str], out_dir: str, pairs_dir: str,
              percentile: float, min_tokens: int) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    manifest: dict = {
        "generated_ts": int(time.time()),
        "relatedness": "real dependency-graph edge (call/reference)",
        "adversarial_cut": f"bottom {percentile}% of edges by body-token Jaccard",
        "params": {"percentile": percentile, "min_tokens": min_tokens},
        "repos": [], "total_gap": 0,
    }
    for r in repos:
        name = os.path.basename(os.path.abspath(r).rstrip("/"))
        print(f"building gap set for {name} ...", flush=True)
        index = index_repository(os.path.abspath(r))
        pairs, stats = build_gap_set(index, name, percentile, min_tokens,
                                     load_cochange(name, pairs_dir))
        out_path = os.path.join(out_dir, name + ".jsonl")
        with open(out_path, "w", encoding="utf-8") as f:
            for p in pairs:
                f.write(json.dumps(asdict(p)) + "\n")
        stats["out"] = os.path.relpath(out_path)
        manifest["repos"].append(stats)
        manifest["total_gap"] += len(pairs)
        print(f"  {stats['n_edge_pairs']} real-edge pairs -> {stats['n_gap']} gap pairs "
              f"(cut J<={stats['jaccard_cut']}); {stats['n_gap_cross_file']} cross-file, "
              f"{stats['n_gap_co_changed']} also co-changed")

    man_path = os.path.join(out_dir, "manifest.json")
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n{manifest['total_gap']} total gap pairs across {len(repos)} repo(s)"
          f" -> {os.path.relpath(man_path)}")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="+", help="paths to git repo clones")
    ap.add_argument("--out-dir", default=GAP_DIR)
    ap.add_argument("--pairs-dir", default=PAIRS_DIR,
                    help="Item-1 dataset dir, for the co_changed annotation")
    ap.add_argument("--percentile", type=float, default=DEFAULT_PERCENTILE,
                    help="keep edges in the bottom this%% of lexical overlap "
                         "(default %(default)s)")
    ap.add_argument("--min-tokens", type=int, default=DEFAULT_MIN_TOKENS,
                    help="skip symbols with fewer domain tokens (default %(default)s)")
    args = ap.parse_args()
    write_gap(args.repos, args.out_dir, args.pairs_dir, args.percentile, args.min_tokens)


if __name__ == "__main__":
    main()
