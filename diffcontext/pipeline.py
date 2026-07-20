"""
pipeline.py — The main DiffContext pipeline.

Connects all stages: parse -> graph -> diff -> blast radius -> score -> select -> compile

Key fixes vs original:
  - expanded_deps is now passed into compute_impact_scores so those symbols
    are actually scored (previously they were collected but never used).
  - Graph is built with a single pass and cached import maps (see graph_builder).
  - warn_unknown_symbols is called before any scoring to surface typos early.
"""

import ast
import difflib
import logging
import os
from typing import Callable, Dict, List, Optional

from .impact.scoring import ScoringConfig

from .models import (
    RepositoryIndex, ImpactResult, ContextPackage, Symbol,
)
from .languages import available_adapters, discover_files
from .parser import extract_symbols
from .scanner import find_python_files
from .cache import SymbolCache, get_file_hash, hash_source, repo_state_hash
from .resolver import build_import_map
from .graph_builder import build_repository_graph
from ._warn_once import warn_syntax_error_once, check_and_warn_encoding, WarnState
from .impact.blast_radius import get_blast_radius
from .impact.scoring import compute_impact_scores
from .impact.traversal import expand_dependencies
from .context.selector import select_context
from .context.compiler import compile_context

logger = logging.getLogger(__name__)


def _suggest_similar_symbol(unknown_id: str, known_ids) -> Optional[str]:
    """
    Fuzzy-match an unknown symbol ID against known ones (typo correction).

    Not plain difflib.get_close_matches over full IDs: symbol IDs share
    long path prefixes, so difflib's quick-ratio prefilters pass nearly
    every candidate and it computes a full ratio against the whole index
    per unknown symbol (measured: ~2s per unknown on a 9k-symbol index —
    dominated `verify --from-history` on a repo whose recent commits
    renamed benchmark symbols). Instead, match on the name part:
      1. exact name match first — a symbol that moved files (the common
         churn case) resolves with a dict lookup, no fuzz at all;
      2. otherwise fuzz over UNIQUE name parts only — short strings with
         no shared prefixes, where the prefilters actually discriminate —
         then pick the closest full ID within the winning name.
    """
    known_list = list(known_ids)
    name = unknown_id.rsplit(":", 1)[1] if ":" in unknown_id else unknown_id

    by_name: dict = {}
    for k in known_list:
        by_name.setdefault(k.rsplit(":", 1)[-1], []).append(k)

    same_name = by_name.get(name)
    if same_name:
        # moved/renamed file — pick the closest full ID among exact
        # name matches (tiny pool, full difflib is fine here)
        matches = difflib.get_close_matches(unknown_id, same_name, n=1, cutoff=0.0)
        return matches[0]

    # Typo in the name part: fuzz over unique names only. Short strings
    # without shared path prefixes let difflib's quick-ratio prefilters
    # actually discriminate, and the pool is unique names, not every ID.
    close = difflib.get_close_matches(name, by_name.keys(), n=1, cutoff=0.6)
    if not close:
        return None
    matches = difflib.get_close_matches(unknown_id, by_name[close[0]], n=1, cutoff=0.0)
    return matches[0] if matches else None


def _read_and_parse(
    filename: str,
    repo_path: str,
    broken_files: List[str],
    warn_state: Optional[WarnState] = None,
):
    """
    Read + parse one file exactly once. Returns (rel_file, source, tree,
    content_hash); tree is None (and rel_file is appended to broken_files)
    on SyntaxError.
    """
    rel_file = "./" + os.path.relpath(filename, repo_path)
    with open(filename, "rb") as f:
        raw = f.read()
    check_and_warn_encoding(logger, filename, raw, state=warn_state)
    source = raw.decode("utf-8", errors="ignore")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        warn_syntax_error_once(logger, filename, e, state=warn_state)
        broken_files.append(rel_file)
        return rel_file, source, None, hash_source(raw)
    return rel_file, source, tree, hash_source(raw)


def index_repository(repo_path: str) -> RepositoryIndex:
    """
    Phase 1: Parse repository and build dependency graph.

    Each file is read and parsed exactly ONCE per process (symbol
    extraction, import maps, and the graph builder all share the same AST).
    The finished graph is persisted content-addressed (keyed by the combined
    hash of every file), so re-indexing an unchanged repo — even from a new
    process — skips parsing and graph construction entirely.

    Returns a RepositoryIndex with all symbols, the call graph, and the
    list of files (if any) that failed to parse due to a SyntaxError. The
    returned index supports in-process incremental updates via
    `index.update(changed_files)`.
    """
    repo_path = os.path.abspath(repo_path)
    db_path = os.path.join(repo_path, ".diffcontext_cache.db")

    files = find_python_files(repo_path)

    # Optional language adapters (languages/): each contributes its own
    # files, symbols, and edges. Absent extras mean empty dicts here and
    # a pipeline identical to the Python-only one.
    adapter_files: Dict[object, List[str]] = {}
    for adapter in available_adapters():
        found = discover_files(adapter, repo_path)
        if found:
            adapter_files[adapter] = found

    # Session-scoped warn de-dup: one indexing session's warnings must not
    # suppress another's in a long-lived process serving many repos.
    warn_state = WarnState()
    broken_files: List[str] = []
    file_trees: Optional[Dict[str, ast.Module]] = None
    import_maps: Optional[Dict[str, Dict[str, str]]] = None
    lang_graphs: Optional[Dict[str, Dict[str, List[str]]]] = None

    with SymbolCache(db_path) as cache:
        # Read + hash every file (one disk pass). Parsing is deferred until
        # we know the graph cache missed — on a hit, no file is parsed at
        # all and symbols come straight from the symbol cache. The state
        # hash covers adapter-language files too: a .ts edit must miss the
        # cached graph exactly like a .py edit.
        raw_bytes: Dict[str, bytes] = {}       # rel -> file contents (.py)
        rel_to_abs: Dict[str, str] = {}
        file_hashes: Dict[str, str] = {}
        for filename in files:
            rel = "./" + os.path.relpath(filename, repo_path)
            with open(filename, "rb") as f:
                raw = f.read()
            raw_bytes[rel] = raw
            rel_to_abs[rel] = filename
            file_hashes[rel] = hash_source(raw)

        lang_sources: Dict[object, Dict[str, str]] = {}  # adapter -> rel -> text
        lang_rel_to_abs: Dict[str, str] = {}
        for adapter, afiles in adapter_files.items():
            sources: Dict[str, str] = {}
            for filename in afiles:
                rel = "./" + os.path.relpath(filename, repo_path)
                with open(filename, "rb") as f:
                    raw = f.read()
                file_hashes[rel] = hash_source(raw)
                lang_rel_to_abs[rel] = filename
                sources[rel] = raw.decode("utf-8", errors="ignore")
            lang_sources[adapter] = sources

        state_hash = repo_state_hash(file_hashes)
        cached = cache.get_graph(state_hash)

        symbols: Dict[str, Symbol] = {}

        if cached is not None:
            # Warm path: graph and broken-file list restored from cache;
            # symbols served from the symbol cache (same content hashes, so
            # every lookup is a hit — zero parsing).
            graph, broken_files = cached
            for rel, filename in rel_to_abs.items():
                def _parse(path):
                    return extract_symbols(path, repo_path)
                symbols.update(cache.get_or_parse(
                    filename, _parse, known_hash=file_hashes[rel]
                ))
            for adapter, sources in lang_sources.items():
                for rel, text in sources.items():
                    def _parse_lang(path, _src=text, _ad=adapter):
                        return _ad.extract_file_symbols(path, repo_path, _src)
                    symbols.update(cache.get_or_parse(
                        lang_rel_to_abs[rel], _parse_lang,
                        known_hash=file_hashes[rel],
                    ))
        else:
            # Cold path: parse each file exactly once; symbol extraction,
            # import maps, and the graph builder all share the same AST.
            parsed: Dict[str, tuple] = {}      # rel -> (abs, source, tree)
            for rel, raw in raw_bytes.items():
                filename = rel_to_abs[rel]
                check_and_warn_encoding(logger, filename, raw, state=warn_state)
                source = raw.decode("utf-8", errors="ignore")
                try:
                    tree = ast.parse(source)
                except SyntaxError as e:
                    warn_syntax_error_once(logger, filename, e, state=warn_state)
                    broken_files.append(rel)
                    continue
                parsed[rel] = (filename, source, tree)

            for rel, (filename, source, tree) in parsed.items():
                def _parse(path, _src=source, _tree=tree):
                    return extract_symbols(path, repo_path, source=_src, tree=_tree)
                symbols.update(cache.get_or_parse(
                    filename, _parse, known_hash=file_hashes[rel]
                ))

            file_trees = {rel: t for rel, (_f, _s, t) in parsed.items()}
            import_maps = {
                rel: build_import_map(f, repo_path, tree=t)
                for rel, (f, _s, t) in parsed.items()
            }
            graph = build_repository_graph(
                repo_path,
                functions=symbols,
                file_trees=file_trees,
                import_maps=import_maps,
            )

            lang_graphs = {}
            for adapter, sources in lang_sources.items():
                for rel, text in sources.items():
                    def _parse_lang(path, _src=text, _ad=adapter):
                        return _ad.extract_file_symbols(path, repo_path, _src)
                    symbols.update(cache.get_or_parse(
                        lang_rel_to_abs[rel], _parse_lang,
                        known_hash=file_hashes[rel],
                    ))
                edges = adapter.build_language_graph(repo_path, sources)
                lang_graphs[adapter.name] = edges
                graph.update(edges)   # id namespaces are disjoint by file ext

            cache.put_graph(state_hash, graph, broken_files)

    index = RepositoryIndex(symbols=symbols, graph=graph, broken_files=broken_files)
    # Incremental-update state (private; used by index.update()).
    index._repo_path = repo_path
    index._file_trees = file_trees      # None on graph-cache hit (lazy)
    index._import_maps = import_maps    # None on graph-cache hit (lazy)
    index._lang_graphs = lang_graphs    # None on graph-cache hit (lazy)
    index._warn_state = warn_state
    return index


def _ensure_trees(index: RepositoryIndex) -> None:
    """Materialize per-file ASTs/import maps if the index was loaded from
    the graph cache (which stores no trees). One-time cost, then reused."""
    if index._file_trees is not None:
        return
    repo_path = index._repo_path
    broken: List[str] = []
    trees: Dict[str, ast.Module] = {}
    for filename in find_python_files(repo_path):
        rel, _source, tree, _h = _read_and_parse(
            filename, repo_path, broken, warn_state=index._warn_state
        )
        if tree is not None:
            trees[rel] = tree
    index._file_trees = trees
    index._import_maps = {
        rel: build_import_map(os.path.join(repo_path, rel[2:]), repo_path, tree=t)
        for rel, t in trees.items()
    }


def _normalize_changed_files(repo_path: str, changed_files: List[str]) -> list:
    """Normalize user-supplied paths to (abs_path, "./rel") pairs."""
    normalized = []
    for path in changed_files:
        abs_path = path if os.path.isabs(path) else os.path.join(repo_path, path.lstrip("./"))
        abs_path = os.path.abspath(abs_path)
        rel = "./" + os.path.relpath(abs_path, repo_path)
        normalized.append((abs_path, rel))
    return normalized


def _needs_full_map_rebuild(py_normalized, trees, broken_files) -> bool:
    """An added or deleted module can change how OTHER files' imports
    resolve; an edited __init__.py can change re-export resolution.
    In those cases every import map must be rebuilt; otherwise only the
    changed files' maps."""
    return any(
        not os.path.exists(abs_path)                  # deleted
        or rel not in trees and rel not in broken_files  # created
        or os.path.basename(rel) == "__init__.py"
        for abs_path, rel in py_normalized
    )


def _refresh_changed_python_files(index, cache, py_normalized, full_map_rebuild):
    """Drop each changed file's previous symbols/trees/maps, then re-read,
    re-parse, and re-extract it (deleted files are only dropped)."""
    repo_path = index._repo_path
    trees = index._file_trees
    import_maps = index._import_maps
    for abs_path, rel in py_normalized:
        stale_ids = [sid for sid in index.symbols if sid.startswith(rel + ":")]
        for sid in stale_ids:
            del index.symbols[sid]
        trees.pop(rel, None)
        import_maps.pop(rel, None)
        if rel in index.broken_files:
            index.broken_files.remove(rel)

        if not os.path.exists(abs_path):
            continue   # deleted file: nothing to re-add

        broken: List[str] = []
        _rel, source, tree, _h = _read_and_parse(
            abs_path, repo_path, broken, warn_state=index._warn_state
        )
        if tree is None:
            index.broken_files.extend(broken)
            continue

        trees[rel] = tree
        def _parse(path, _src=source, _tree=tree):
            return extract_symbols(path, repo_path, source=_src, tree=_tree)
        index.symbols.update(cache.get_or_parse(abs_path, _parse))
        if not full_map_rebuild:
            import_maps[rel] = build_import_map(abs_path, repo_path, tree=tree)


def _rebuild_all_import_maps(index) -> None:
    repo_path = index._repo_path
    index._import_maps = {
        rel: build_import_map(
            os.path.join(repo_path, rel[2:]), repo_path, tree=t
        )
        for rel, t in index._file_trees.items()
    }


def _update_language_parts(index, cache, lang_changed_rels) -> Dict[str, str]:
    """
    Rebuild language-adapter graph parts. Import/barrel effects are
    cross-file, so when one of an adapter's files changed its whole part
    is rebuilt (a full tree-sitter pass is cheap); unchanged parts are
    reused from the previous build. A warm-started index (graph-cache
    hit) has no cached parts and rebuilds them once here.

    Returns rel -> content hash for every adapter-language file, for the
    repo state hash.
    """
    repo_path = index._repo_path
    lang_graphs = index._lang_graphs if index._lang_graphs is not None else {}
    adapter_file_hashes: Dict[str, str] = {}
    for adapter in available_adapters():
        exts = tuple(adapter.extensions)
        raws: Dict[str, bytes] = {}
        for fpath in discover_files(adapter, repo_path):
            rel = "./" + os.path.relpath(fpath, repo_path)
            with open(fpath, "rb") as fh:
                raw = fh.read()
            adapter_file_hashes[rel] = hash_source(raw)
            raws[rel] = raw

        rebuild = index._lang_graphs is None or any(
            r.endswith(exts) for r in lang_changed_rels
        )
        had_symbols = any(
            sid.split(":", 1)[0].endswith(exts) for sid in index.symbols
        )
        if not rebuild or not (raws or had_symbols):
            continue

        stale = [
            sid for sid in index.symbols
            if sid.split(":", 1)[0].endswith(exts)
        ]
        for sid in stale:
            del index.symbols[sid]

        sources: Dict[str, str] = {}
        for rel, raw in raws.items():
            text = raw.decode("utf-8", errors="ignore")
            sources[rel] = text
            def _parse_lang(path, _src=text, _ad=adapter):
                return _ad.extract_file_symbols(path, repo_path, _src)
            index.symbols.update(cache.get_or_parse(
                os.path.join(repo_path, rel[2:]), _parse_lang,
                known_hash=adapter_file_hashes[rel],
            ))
        lang_graphs[adapter.name] = adapter.build_language_graph(
            repo_path, sources
        )
    index._lang_graphs = lang_graphs
    for edges in lang_graphs.values():
        index.graph.update(edges)
    return adapter_file_hashes


def _persist_graph_cache(cache, index, adapter_file_hashes) -> None:
    """Persist the new state so future processes get a warm start too."""
    repo_path = index._repo_path
    file_hashes = {
        "./" + os.path.relpath(f, repo_path): get_file_hash(f)
        for f in find_python_files(repo_path)
    }
    file_hashes.update(adapter_file_hashes)
    cache.put_graph(repo_state_hash(file_hashes), index.graph, list(index.broken_files))


def update_index(index: RepositoryIndex, changed_files: List[str]) -> RepositoryIndex:
    """
    Incrementally update an index after `changed_files` were edited,
    created, or deleted — without re-reading or re-parsing any other file.

    Only the changed files are re-parsed; their symbols are re-extracted
    (and re-cached), their import maps rebuilt, and the graph is then
    rebuilt from the in-memory ASTs. Edge resolution is repo-wide (cross-
    file edges make per-edge incrementality unsound), but all file I/O and
    parsing is strictly limited to the changed files.

    Accepts absolute paths, or paths relative to the repo root (with or
    without the "./" prefix). Returns the same index, mutated in place.
    """
    if index._repo_path is None:
        raise ValueError(
            "This index does not support update(): it was not created by "
            "index_repository()."
        )
    repo_path = index._repo_path
    db_path = os.path.join(repo_path, ".diffcontext_cache.db")

    _ensure_trees(index)

    normalized = _normalize_changed_files(repo_path, changed_files)
    # Adapter-language files (".ts" etc.) are handled by their adapter —
    # the per-file Python flow must not try to ast.parse them.
    py_normalized = [(a, r) for a, r in normalized if r.endswith(".py")]
    lang_changed_rels = [r for _a, r in normalized if not r.endswith(".py")]

    full_map_rebuild = _needs_full_map_rebuild(
        py_normalized, index._file_trees, index.broken_files
    )

    with SymbolCache(db_path) as cache:
        _refresh_changed_python_files(index, cache, py_normalized, full_map_rebuild)
        if full_map_rebuild:
            _rebuild_all_import_maps(index)

        # Symbols changed: the cached BM25 index no longer matches them.
        index._lexical = None

        # Rebuild graph from in-memory state (no file I/O, no parsing);
        # the cached reverse graph goes stale with it.
        index._reverse_graph = None
        index.graph = build_repository_graph(
            repo_path,
            functions=index.symbols,
            file_trees=index._file_trees,
            import_maps=index._import_maps,
        )

        adapter_file_hashes = _update_language_parts(index, cache, lang_changed_rels)
        _persist_graph_cache(cache, index, adapter_file_hashes)

    return index


def warn_unknown_symbols(index: RepositoryIndex, changed_symbols: List[str]) -> List[str]:
    """
    Check `changed_symbols` against the index and warn about any that don't
    actually exist. Returns the list of unknown symbol IDs.
    """
    unknown = [s for s in changed_symbols if s not in index.graph and s not in index.symbols]
    for sym_id in unknown:
        suggestion = _suggest_similar_symbol(sym_id, index.symbols.keys())
        if suggestion:
            logger.warning(
                "\033[93m'%s' was not found in the index -- did you mean '%s'? "
                "Its blast radius will show as empty, which does NOT mean "
                "the real symbol has no impact.\033[0m",
                sym_id, suggestion,
            )
        else:
            logger.warning(
                "\033[93m'%s' was not found in the index (typo, renamed, or "
                "deleted symbol?). Its blast radius will show as empty, "
                "which does NOT mean the real symbol has no impact.\033[0m",
                sym_id,
            )
    return unknown


def _normalize_scores(scores: Dict[str, float]) -> Dict[str, float]:
    """Min-max normalize a score dict to [0, 1]."""
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    if hi == lo:
        return {k: 0.5 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


# Hybrid blend weights (graph, lexical/BM25, same-file). These are the
# leave-one-repo-out-validated values from the 2026-07 rigor pass
# (benchmarks/RIGOR_REPORT_2026-07.md §3): the original same-repo-tuned
# (0.5, 0.35, 0.15) over-weighted the graph; every LORO fold selects a
# BM25-heavier blend, and (0.3, 0.5, 0.2) beat the old weights on 4/5
# held-out folds (+1.2 to +2.4 recall points, individually n.s.) while
# staying within ±1.1 points on four repos never used for any selection.
# Change only with benchmark evidence.
HYBRID_WEIGHTS = (0.3, 0.5, 0.2)

# Number of graph-scored candidates at which graph confidence saturates
# to 1.0 for the adaptive blend. Below it, graph weight is scaled down
# proportionally and the freed weight moves to BM25 — a sparse blast
# radius means the graph has little to say and lexical similarity is the
# better bet (the measured "thematic siblings" blind spot).
ADAPTIVE_GRAPH_SATURATION = 8


def _adaptive_weights(n_graph_candidates: int, weights=HYBRID_WEIGHTS):
    """Shift weight from the graph signal to BM25 when the graph produced
    few candidates. With >= ADAPTIVE_GRAPH_SATURATION graph candidates the
    result equals `weights` exactly (no behavior change on well-connected
    changes)."""
    w_graph, w_lex, w_file = weights
    confidence = min(1.0, n_graph_candidates / ADAPTIVE_GRAPH_SATURATION)
    w_graph_eff = w_graph * confidence
    return (w_graph_eff, w_lex + (w_graph - w_graph_eff), w_file)


def _blend_hybrid(
    index: RepositoryIndex,
    changed_symbols: List[str],
    graph_scores: Dict[str, float],
    weights=HYBRID_WEIGHTS,
    adaptive: bool = True,
    history_scores: Optional[Dict[str, float]] = None,
    history_weight: float = 0.15,
) -> Dict[str, float]:
    """
    Blend graph impact scores with BM25 and same-file signals.

    Changed symbols keep their original (top) score; every other candidate
    gets `100 * (w_g*graph + w_b*bm25 + w_f*samefile)` where each signal is
    min-max normalized to [0, 1]. A symbol with no call-graph connection to
    the change can still surface through lexical similarity or co-location —
    the two failure modes where the graph alone is blind.

    With `adaptive=True` (default) the graph weight is scaled by graph
    confidence: when the blast radius produced few candidates, the freed
    weight moves to BM25 (see _adaptive_weights). On well-connected
    changes the weights are exactly `weights` — no behavior change.

    `history_scores` (per-file git co-change association in [0, 1], from
    diffcontext.history) is an optional fourth signal: every symbol in a
    file that historically co-changed with the changed files gets
    `history_weight * association` added — the only signal that can reach
    co-change partners with no structural or lexical connection at all.
    """
    from .lexical import get_lexical_index

    changed_set = set(changed_symbols)
    changed_in_index = [s for s in changed_symbols if s in index.symbols]
    if not changed_in_index:
        return graph_scores

    graph_norm = _normalize_scores(
        {s: sc for s, sc in graph_scores.items() if s not in changed_set}
    )

    if adaptive:
        weights = _adaptive_weights(len(graph_norm), weights)
    w_graph, w_lex, w_file = weights

    # Lexical: max BM25 score against any changed symbol's code
    lex_raw: Dict[str, float] = {}
    lexical_index = get_lexical_index(index)
    for sym_id in changed_in_index:
        for sid, sc in lexical_index.scores_for(index.symbols[sym_id].code).items():
            if sid not in changed_set and sc > lex_raw.get(sid, 0.0):
                lex_raw[sid] = sc
    lex_norm = _normalize_scores(lex_raw)

    changed_files = {s.split(":")[0] for s in changed_in_index}

    history_files = {
        f for f, sc in (history_scores or {}).items() if sc > 0.0
    }

    blended: Dict[str, float] = {}
    candidates = set(graph_norm) | set(lex_norm)
    candidates.update(
        sid for sid in index.symbols
        if sid.split(":")[0] in changed_files and sid not in changed_set
    )
    if history_files:
        candidates.update(
            sid for sid in index.symbols
            if sid.split(":")[0] in history_files and sid not in changed_set
        )
    for sid in candidates:
        score = w_graph * graph_norm.get(sid, 0.0) + w_lex * lex_norm.get(sid, 0.0)
        sid_file = sid.split(":")[0]
        if sid_file in changed_files:
            score += w_file
        if history_scores:
            score += history_weight * history_scores.get(sid_file, 0.0)
        blended[sid] = 100.0 * score

    # Changed symbols keep their unblended score so they stay ranked on top.
    for sym_id in changed_symbols:
        if sym_id in graph_scores:
            blended[sym_id] = graph_scores[sym_id]
    return blended


def analyze_impact(
    index: RepositoryIndex,
    changed_symbols: List[str],
    max_depth: Optional[int] = 2,
    scoring_config: Optional["ScoringConfig"] = None,
    hybrid: bool = True,
    adaptive: bool = True,
    history: Optional[object] = None,
) -> ImpactResult:
    """
    Phase 2: Given changed symbols, compute blast radius and impact scores.

    By default scores are the hybrid blend of call-graph impact, BM25
    lexical similarity, and same-file co-location — the configuration that
    won the eval_v2 benchmark on every repo tested. Pass hybrid=False for
    the graph-only signal (e.g. for blast-radius verification, where only
    real call edges should count).

    adaptive: scale the graph weight by graph confidence — when the blast
        radius produced few candidates, the freed weight moves to BM25.
        Identical to the fixed blend on well-connected changes.
    history:  optional diffcontext.history.CoChangeIndex. When given, git
        co-change association is blended as a fourth signal — the only
        signal that can reach co-change partners with no structural or
        lexical connection (the measured cross-subsystem ceiling).

    Fix: expanded_deps is now passed into compute_impact_scores so those
    nodes are actually scored. Previously they were computed and discarded.
    """
    warn_unknown_symbols(index, changed_symbols)

    # ── Blast radius (reverse graph / callers) ────────────────────────────
    reverse = index.reverse_graph
    blast_radii: Dict[str, List[str]] = {}
    all_blast: List[str] = []

    for sym_id in changed_symbols:
        if sym_id in index.graph:
            radius = get_blast_radius(index.graph, sym_id, reverse=reverse)
            blast_radii[sym_id] = radius
            all_blast.extend(radius)

    # ── Forward dependency expansion ──────────────────────────────────────
    # Seed with changed + blast so we also pull in what callers depend on.
    deps = expand_dependencies(
        index.graph,
        changed_symbols + all_blast,
        max_depth=max_depth,
    )

    # ── Impact scoring ────────────────────────────────────────────────────
    # FIX: pass expanded deps so they get scored (previously ignored).
    scores = compute_impact_scores(
        index.graph,
        changed_symbols,
        blast_radii,
        expanded_deps=deps,
        reverse=reverse,
        config=scoring_config,
    )

    # ── Hybrid blend (graph + BM25 + same-file [+ history]) ──────────────
    if hybrid:
        history_scores = (
            history.scores_for_symbols(changed_symbols)
            if history is not None else None
        )
        scores = _blend_hybrid(
            index, changed_symbols, scores,
            adaptive=adaptive, history_scores=history_scores,
        )

    return ImpactResult(
        changed=changed_symbols,
        blast_radius=list(set(all_blast)),
        dependencies=deps,
        scores=scores,
    )


def compile(
    index: RepositoryIndex,
    impact: ImpactResult,
    max_tokens: Optional[int] = 10000,
    notes: Optional[str] = None,
    token_counter: Optional[Callable[[str], int]] = None,
    scoring_config: Optional["ScoringConfig"] = None,
    top_k: Optional[int] = None,
    cutoff: Optional[str] = None,
) -> ContextPackage:
    """
    Phase 3: Select symbols and compile into LLM context.

    Args:
        token_counter:  Optional text -> token count callable. Pass your
                        model's real tokenizer when enforcing a hard window
                        limit; defaults to the ~4-chars/token heuristic.
        scoring_config: The ScoringConfig used in analyze_impact (if any),
                        so the meta-header describes the actual run.
        top_k:          Optional cap on non-changed symbols, applied on top
                        of the token budget (see select_context; ~20 per
                        changed symbol is the benchmarked sweet spot).
        cutoff:         "gap" applies the largest-gap dynamic cutoff before
                        top_k and the budget — the measured precision
                        operating point (~4x top-20 precision at 6-9
                        symbols, ~30% relative recall cost; see
                        benchmarks/RIGOR_REPORT_2026-07.md §7). Default
                        None keeps recall-first top-k selection.
    """
    selected, dropped = select_context(
        index.symbols,
        impact.scores,
        impact.changed,
        max_tokens=max_tokens,
        token_counter=token_counter,
        top_k=top_k,
        graph=index.graph,
        reverse=index.reverse_graph,
        cutoff=cutoff,
    )

    return compile_context(
        index.symbols,
        selected,
        impact.changed,
        impact.scores,
        graph=index.graph,
        reverse=index.reverse_graph,
        dropped_ids=dropped,
        skipped_files=index.broken_files,
        notes=notes,
        token_counter=token_counter,
        scoring_config=scoring_config,
        max_tokens=max_tokens,
    )


def run_pipeline(
    repo_path: str,
    changed_symbols: List[str],
    max_depth: Optional[int] = 2,
    max_tokens: Optional[int] = 10000,
    cutoff: Optional[str] = None,
) -> ContextPackage:
    """
    Full pipeline in one call:
        repo_path + changed_symbols -> ContextPackage
    """
    index = index_repository(repo_path)
    impact = analyze_impact(index, changed_symbols, max_depth=max_depth)
    return compile(index, impact, max_tokens=max_tokens, cutoff=cutoff)