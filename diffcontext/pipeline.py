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
import time
from typing import Callable, Dict, List, Optional

from .impact.scoring import ScoringConfig

from .models import (
    RepositoryIndex, ImpactResult, ContextPackage, Symbol,
)
from .parser import extract_all_symbols, extract_symbols
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
    """Fuzzy-match an unknown symbol ID against known ones (typo correction)."""
    matches = difflib.get_close_matches(unknown_id, known_ids, n=1, cutoff=0.6)
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

    # Session-scoped warn de-dup: one indexing session's warnings must not
    # suppress another's in a long-lived process serving many repos.
    warn_state = WarnState()
    broken_files: List[str] = []
    file_trees: Optional[Dict[str, ast.Module]] = None
    import_maps: Optional[Dict[str, Dict[str, str]]] = None

    with SymbolCache(db_path) as cache:
        # Read + hash every file (one disk pass). Parsing is deferred until
        # we know the graph cache missed — on a hit, no file is parsed at
        # all and symbols come straight from the symbol cache.
        raw_bytes: Dict[str, bytes] = {}       # rel -> file contents
        rel_to_abs: Dict[str, str] = {}
        file_hashes: Dict[str, str] = {}
        for filename in files:
            rel = "./" + os.path.relpath(filename, repo_path)
            with open(filename, "rb") as f:
                raw = f.read()
            raw_bytes[rel] = raw
            rel_to_abs[rel] = filename
            file_hashes[rel] = hash_source(raw)

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
                symbols.update(cache.get_or_parse(filename, _parse))
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
                symbols.update(cache.get_or_parse(filename, _parse))

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
            cache.put_graph(state_hash, graph, broken_files)

    index = RepositoryIndex(symbols=symbols, graph=graph, broken_files=broken_files)
    # Incremental-update state (private; used by index.update()).
    index._repo_path = repo_path
    index._file_trees = file_trees      # None on graph-cache hit (lazy)
    index._import_maps = import_maps    # None on graph-cache hit (lazy)
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
    trees = index._file_trees
    import_maps = index._import_maps

    # Normalize inputs to (abs, "./rel") pairs
    normalized = []
    for path in changed_files:
        abs_path = path if os.path.isabs(path) else os.path.join(repo_path, path.lstrip("./"))
        abs_path = os.path.abspath(abs_path)
        rel = "./" + os.path.relpath(abs_path, repo_path)
        normalized.append((abs_path, rel))

    # An added or deleted module can change how OTHER files' imports
    # resolve; an edited __init__.py can change re-export resolution.
    # In those cases rebuild every import map; otherwise only the changed
    # files' maps.
    full_map_rebuild = any(
        not os.path.exists(abs_path)                  # deleted
        or rel not in trees and rel not in index.broken_files  # created
        or os.path.basename(rel) == "__init__.py"
        for abs_path, rel in normalized
    )

    with SymbolCache(db_path) as cache:
        for abs_path, rel in normalized:
            # Drop this file's previous symbols/trees/maps
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

        if full_map_rebuild:
            index._import_maps = import_maps = {
                rel: build_import_map(
                    os.path.join(repo_path, rel[2:]), repo_path, tree=t
                )
                for rel, t in trees.items()
            }

        # Rebuild graph from in-memory state (no file I/O, no parsing)
        index.graph = build_repository_graph(
            repo_path,
            functions=index.symbols,
            file_trees=trees,
            import_maps=import_maps,
        )

        # Persist the new state so future processes get a warm start too
        file_hashes = {
            "./" + os.path.relpath(f, repo_path): get_file_hash(f)
            for f in find_python_files(repo_path)
        }
        cache.put_graph(repo_state_hash(file_hashes), index.graph, list(index.broken_files))

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


def analyze_impact(
    index: RepositoryIndex,
    changed_symbols: List[str],
    max_depth: Optional[int] = 2,
    scoring_config: Optional["ScoringConfig"] = None,
) -> ImpactResult:
    """
    Phase 2: Given changed symbols, compute blast radius and impact scores.

    Fix: expanded_deps is now passed into compute_impact_scores so those
    nodes are actually scored. Previously they were computed and discarded.
    """
    warn_unknown_symbols(index, changed_symbols)

    # ── Blast radius (reverse graph / callers) ────────────────────────────
    blast_radii: Dict[str, List[str]] = {}
    all_blast: List[str] = []

    for sym_id in changed_symbols:
        if sym_id in index.graph:
            radius = get_blast_radius(index.graph, sym_id)
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
        config=scoring_config,
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
) -> ContextPackage:
    """
    Phase 3: Select symbols and compile into LLM context.

    Args:
        token_counter:  Optional text -> token count callable. Pass your
                        model's real tokenizer when enforcing a hard window
                        limit; defaults to the ~4-chars/token heuristic.
        scoring_config: The ScoringConfig used in analyze_impact (if any),
                        so the meta-header describes the actual run.
    """
    selected, dropped = select_context(
        index.symbols,
        impact.scores,
        impact.changed,
        max_tokens=max_tokens,
        token_counter=token_counter,
    )

    return compile_context(
        index.symbols,
        selected,
        impact.changed,
        impact.scores,
        graph=index.graph,
        dropped_ids=dropped,
        skipped_files=index.broken_files,
        notes=notes,
        token_counter=token_counter,
        scoring_config=scoring_config,
    )


def run_pipeline(
    repo_path: str,
    changed_symbols: List[str],
    max_depth: Optional[int] = 2,
    max_tokens: Optional[int] = 10000,
) -> ContextPackage:
    """
    Full pipeline in one call:
        repo_path + changed_symbols -> ContextPackage
    """
    index = index_repository(repo_path)
    impact = analyze_impact(index, changed_symbols, max_depth=max_depth)
    return compile(index, impact, max_tokens=max_tokens)