"""
features.py — the stage-2 feature extractor.

Given the symbol table, the call graph + reverse graph, the changed symbols
and one candidate, emit a fixed-order ``List[float]``. Everything here is
derivable from structures the pipeline already builds: no new dependencies,
no new parsing passes, no file re-reads.

Two signals are *optional inputs* rather than hard requirements, because the
pipeline only has them in some configurations:

  * ``import_overlap``  needs ``index._import_maps`` (present after
    ``index_repository``; ``None`` on a graph-cache warm start)
  * ``cochange_assoc``  needs a ``CoChangeIndex`` (only with ``--with-history``)

Both degrade to ``0.0`` when absent. That is deliberate: a missing signal must
look like "no evidence", never like a different scale, or a model trained with
history would silently mis-score a run without it.

FEATURE_ORDER IS PART OF THE MODEL CONTRACT. ``weights.json`` stores the names
and ``model.py`` asserts them on load. Appending a feature requires retraining
and a version bump; reordering silently corrupts every score.

Cost: the per-query precompute (three bounded BFS passes + a BM25 rank sort)
dominates; per-candidate extraction is dict lookups and two set intersections.
Measured budget in docs/RERANK.md.
"""

import math
import os
import re
from collections import deque
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

from ..lexical import tokenize

# Fixed feature order — see module docstring. Changing this is a breaking
# change to weights.json.
FEATURE_NAMES: Sequence[str] = (
    "inv_hop_fwd",          # 1
    "inv_hop_bwd",          # 2
    "inv_hop_undirected",   # 3
    "is_direct_callee",     # 4
    "is_direct_caller",     # 5
    "is_sibling",           # 6
    "is_same_file",         # 7
    "is_same_class",        # 8
    "dir_distance",         # 9
    "log_file_n_symbols",   # 10
    "bm25_score",           # 11
    "inv_bm25_rank",        # 12
    "log_cand_indegree",    # 13
    "log_cand_outdegree",   # 14
    "log_cand_tokens",      # 15
    "name_jaccard",         # 16
    "body_token_overlap",   # 17
    "import_overlap",       # 18
    "is_test",              # 19
    "is_private",           # 20
    "is_dunder",            # 21
    "cochange_assoc",       # 22
)

N_FEATURES = len(FEATURE_NAMES)

# Hops beyond this are indistinguishable from unreachable for ranking
# purposes, and bounding the frontier keeps BFS linear on hub-heavy graphs.
MAX_HOPS = 8

# Hard ceiling on nodes visited per BFS direction. django's graph is ~40k
# edges; without a cap one pathological query could dominate the budget.
BFS_NODE_CAP = 30000

_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|[0-9]+")


def split_identifier(name: str) -> Set[str]:
    """Lowercased sub-tokens of a symbol name.

    ``"HTTPAdapter.send_request"`` -> ``{"http", "adapter", "send", "request"}``.
    Splits on ``.``/``_`` and camelCase, so ``sendRequest`` and ``send_request``
    produce the same tokens. Single characters are dropped, matching
    ``lexical.tokenize``.
    """
    out: Set[str] = set()
    for part in re.split(r"[._]+", name):
        for tok in _CAMEL_RE.findall(part):
            if len(tok) > 1:
                out.add(tok.lower())
    return out


def _bare_name(symbol_id: str) -> str:
    """``"./a/b.py:Cls.method"`` -> ``"method"``."""
    name = symbol_id.split(":", 1)[1] if ":" in symbol_id else symbol_id
    return name.rsplit(".", 1)[-1]


def _class_of(symbol_id: str) -> str:
    """``"./a.py:Cls.method"`` -> ``"Cls"``; ``""`` for module-level funcs."""
    name = symbol_id.split(":", 1)[1] if ":" in symbol_id else symbol_id
    return name.rsplit(".", 1)[0] if "." in name else ""


def _file_of(symbol_id: str) -> str:
    return symbol_id.split(":", 1)[0] if ":" in symbol_id else ""


def _dir_distance(file_a: str, file_b: str) -> float:
    """Path-component distance between two files.

    ``0`` for the same directory; otherwise the number of directory levels
    you must walk up from one and back down to the other.
    """
    if file_a == file_b:
        return 0.0
    a = [p for p in os.path.dirname(file_a).split("/") if p not in ("", ".")]
    b = [p for p in os.path.dirname(file_b).split("/") if p not in ("", ".")]
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return float((len(a) - common) + (len(b) - common))


def _bfs_hops(
    sources: Iterable[str],
    adjacency: Mapping[str, Iterable[str]],
    max_hops: int = MAX_HOPS,
    node_cap: int = BFS_NODE_CAP,
) -> Dict[str, int]:
    """Multi-source BFS returning ``node -> min hops from any source``.

    Sources themselves are recorded at hop 0. ``adjacency`` may map to any
    iterable of ids (``graph`` uses lists, ``reverse_graph`` uses sets), so
    it is typed as a ``Mapping`` -- covariant in its value type, unlike
    ``Dict`` -- to accept both without a cast at each call site.
    """
    dist: Dict[str, int] = {s: 0 for s in sources}
    frontier = deque(dist)
    while frontier:
        node = frontier.popleft()
        d = dist[node]
        if d >= max_hops or len(dist) >= node_cap:
            continue
        for nbr in adjacency.get(node, ()) or ():
            if nbr not in dist:
                dist[nbr] = d + 1
                frontier.append(nbr)
    return dist


def _inv_hop(dist: Dict[str, int], sid: str) -> float:
    """``1/(1+hops)``, or 0.0 when unreachable within MAX_HOPS."""
    d = dist.get(sid)
    return 0.0 if d is None else 1.0 / (1.0 + d)


class QueryContext:
    """Everything derivable once per query, so per-candidate work stays O(1)-ish.

    Build one of these per changed-symbol query, then call
    :func:`extract_features` for each candidate.

    Args:
        symbols:        id -> Symbol (needs ``.code``; ``.name`` unused here).
        graph:          id -> [callee ids].
        reverse_graph:  id -> {caller ids}.
        changed:        the changed symbol ids for this query. Only ids
                        present in ``symbols`` contribute.
        bm25_scores:    candidate id -> BM25 score, already maxed over the
                        changed symbols by the caller (both the pipeline and
                        the benchmark harness compute this anyway).
        import_maps:    relative file -> {alias: module}, or None.
        cochange:       file -> association in [0, 1], or None.
        file_counts:    file -> number of symbols in it. Computed from
                        ``symbols`` when omitted; pass it in to share across
                        queries on the same index.
        token_cache:    id -> frozenset of body tokens, shared across queries.
                        Strongly recommended: body tokenization is the single
                        most expensive per-candidate operation, and candidate
                        pools overlap heavily between queries in a repo.
    """

    __slots__ = (
        "symbols", "graph", "reverse_graph", "changed", "bm25_scores",
        "import_maps", "cochange", "file_counts", "token_cache",
        "hop_fwd", "hop_bwd", "hop_und", "direct_callees", "direct_callers",
        "siblings", "changed_files", "changed_classes", "changed_name_tokens",
        "changed_body_tokens", "changed_imports", "bm25_rank",
    )

    def __init__(
        self,
        symbols: Dict[str, object],
        graph: Dict[str, List[str]],
        reverse_graph: Dict[str, Set[str]],
        changed: Sequence[str],
        bm25_scores: Optional[Dict[str, float]] = None,
        import_maps: Optional[Dict[str, Dict[str, str]]] = None,
        cochange: Optional[Dict[str, float]] = None,
        file_counts: Optional[Dict[str, int]] = None,
        token_cache: Optional[Dict[str, frozenset]] = None,
    ):
        self.symbols = symbols
        self.graph = graph
        self.reverse_graph = reverse_graph
        self.bm25_scores = bm25_scores or {}
        self.import_maps = import_maps
        self.cochange = cochange or {}
        self.token_cache = token_cache if token_cache is not None else {}

        live = [c for c in changed if c in symbols]
        self.changed = live

        # --- graph geometry -------------------------------------------------
        # Forward = along callee edges (what the change depends on).
        # Backward = along caller edges (what depends on the change).
        self.hop_fwd = _bfs_hops(live, graph)
        self.hop_bwd = _bfs_hops(live, reverse_graph)

        undirected: Dict[str, Set[str]] = {}
        for node in set(self.hop_fwd) | set(self.hop_bwd):
            nbrs = set(graph.get(node, ()) or ())
            nbrs |= set(reverse_graph.get(node, ()) or ())
            if nbrs:
                undirected[node] = nbrs
        self.hop_und = _bfs_hops(live, undirected)

        self.direct_callees: Set[str] = set()
        self.direct_callers: Set[str] = set()
        for c in live:
            self.direct_callees.update(graph.get(c, ()) or ())
            self.direct_callers.update(reverse_graph.get(c, ()) or ())

        # Siblings: called by something that also calls a changed symbol.
        self.siblings: Set[str] = set()
        for parent in self.direct_callers:
            self.siblings.update(graph.get(parent, ()) or ())
        self.siblings.difference_update(live)

        # --- location + lexical ---------------------------------------------
        self.changed_files = {_file_of(c) for c in live}
        self.changed_classes = {(_file_of(c), _class_of(c)) for c in live}

        self.changed_name_tokens: Set[str] = set()
        for c in live:
            self.changed_name_tokens |= split_identifier(
                c.split(":", 1)[1] if ":" in c else c
            )

        self.changed_body_tokens: Set[str] = set()
        for c in live:
            self.changed_body_tokens |= self._tokens(c)

        self.changed_imports: Set[str] = set()
        if import_maps:
            for f in self.changed_files:
                self.changed_imports.update(
                    (import_maps.get(f) or {}).values()
                )

        # --- BM25 rank (query-comparable, unlike the raw score) -------------
        ranked = sorted(
            self.bm25_scores.items(), key=lambda kv: kv[1], reverse=True
        )
        self.bm25_rank = {sid: i for i, (sid, _) in enumerate(ranked)}

        if file_counts is None:
            file_counts = {}
            for sid in symbols:
                f = _file_of(sid)
                file_counts[f] = file_counts.get(f, 0) + 1
        self.file_counts = file_counts

    def _tokens(self, sid: str) -> frozenset:
        """Body identifier tokens for `sid`, memoized in the shared cache."""
        cached = self.token_cache.get(sid)
        if cached is None:
            sym = self.symbols.get(sid)
            cached = frozenset(tokenize(getattr(sym, "code", "") or ""))
            self.token_cache[sid] = cached
        return cached


def extract_features(ctx: QueryContext, candidate: str) -> List[float]:
    """The 22 features for one candidate, in ``FEATURE_NAMES`` order."""
    cand_file = _file_of(candidate)
    cand_name = candidate.split(":", 1)[1] if ":" in candidate else candidate
    bare = _bare_name(candidate)

    is_same_file = 1.0 if cand_file in ctx.changed_files else 0.0
    is_same_class = 1.0 if (cand_file, _class_of(candidate)) in ctx.changed_classes else 0.0

    # Distance to the *nearest* changed file, so a multi-file change is not
    # penalized by whichever changed file happens to sort first.
    dir_dist = min(
        (_dir_distance(cand_file, f) for f in ctx.changed_files), default=0.0
    )

    cand_tokens = ctx._tokens(candidate)
    body_overlap = 0.0
    if cand_tokens and ctx.changed_body_tokens:
        inter = len(cand_tokens & ctx.changed_body_tokens)
        if inter:
            body_overlap = inter / len(cand_tokens | ctx.changed_body_tokens)

    name_tokens = split_identifier(cand_name)
    name_jaccard = 0.0
    if name_tokens and ctx.changed_name_tokens:
        inter = len(name_tokens & ctx.changed_name_tokens)
        if inter:
            name_jaccard = inter / len(name_tokens | ctx.changed_name_tokens)

    import_overlap = 0.0
    if ctx.changed_imports and ctx.import_maps is not None:
        cand_imports = set((ctx.import_maps.get(cand_file) or {}).values())
        if cand_imports:
            inter = len(cand_imports & ctx.changed_imports)
            if inter:
                import_overlap = inter / len(cand_imports | ctx.changed_imports)

    base = os.path.basename(cand_file)
    is_test = 1.0 if (
        base.startswith("test_") or "/tests/" in cand_file or "/test/" in cand_file
    ) else 0.0
    is_dunder = 1.0 if (bare.startswith("__") and bare.endswith("__")) else 0.0
    is_private = 1.0 if (bare.startswith("_") and not is_dunder) else 0.0

    rank = ctx.bm25_rank.get(candidate)

    return [
        _inv_hop(ctx.hop_fwd, candidate),                                  # 1
        _inv_hop(ctx.hop_bwd, candidate),                                  # 2
        _inv_hop(ctx.hop_und, candidate),                                  # 3
        1.0 if candidate in ctx.direct_callees else 0.0,                   # 4
        1.0 if candidate in ctx.direct_callers else 0.0,                   # 5
        1.0 if candidate in ctx.siblings else 0.0,                         # 6
        is_same_file,                                                      # 7
        is_same_class,                                                     # 8
        dir_dist,                                                          # 9
        math.log1p(ctx.file_counts.get(cand_file, 0)),                     # 10
        ctx.bm25_scores.get(candidate, 0.0),                               # 11
        0.0 if rank is None else 1.0 / (1.0 + rank),                       # 12
        math.log1p(len(ctx.reverse_graph.get(candidate, ()) or ())),       # 13
        math.log1p(len(ctx.graph.get(candidate, ()) or ())),               # 14
        math.log1p(len(cand_tokens)),                                      # 15
        name_jaccard,                                                      # 16
        body_overlap,                                                      # 17
        import_overlap,                                                    # 18
        is_test,                                                           # 19
        is_private,                                                        # 20
        is_dunder,                                                         # 21
        float(ctx.cochange.get(cand_file, 0.0)),                           # 22
    ]
