"""
pipeline.py — The main DiffContext pipeline.

Connects all stages: parse -> graph -> diff -> blast radius -> score -> select -> compile
"""

import difflib
import logging
import os
import time
from typing import Dict, List, Optional

from .models import (
    RepositoryIndex, ImpactResult, ContextPackage, Symbol,
)
from .parser import extract_all_symbols
from .graph_builder import build_repository_graph
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


def index_repository(repo_path: str) -> RepositoryIndex:
    """
    Phase 1: Parse repository and build dependency graph.

    Returns a RepositoryIndex with all symbols, the call graph, and the
    list of files (if any) that failed to parse due to a SyntaxError.
    """
    repo_path = os.path.abspath(repo_path)

    broken_files: List[str] = []
    symbols = extract_all_symbols(repo_path, broken_files=broken_files)
    graph = build_repository_graph(repo_path)

    return RepositoryIndex(symbols=symbols, graph=graph, broken_files=broken_files)


def warn_unknown_symbols(index: RepositoryIndex, changed_symbols: List[str]) -> List[str]:
    """
    Check `changed_symbols` against the index and warn (once per call) about
    any that don't actually exist -- a typo'd symbol ID, or one that was
    renamed/deleted since the index was built. Without this, callers that
    pass an unknown symbol get a result that LOOKS like a real,
    fully-analyzed change with zero callers/dependencies, indistinguishable
    from a real symbol that's genuinely isolated.

    Returns the list of unknown symbol IDs (empty if all were found), so
    callers can decide what to do beyond just warning if they want to.
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
) -> ImpactResult:
    """
    Phase 2: Given changed symbols, compute blast radius and impact scores.

    See warn_unknown_symbols -- any symbol not actually in the index gets
    a clear warning rather than silently scoring as if it were real.
    """
    warn_unknown_symbols(index, changed_symbols)

    # Blast radius for each changed symbol
    blast_radii: Dict[str, List[str]] = {}
    all_blast: List[str] = []

    for sym_id in changed_symbols:
        if sym_id in index.graph:
            radius = get_blast_radius(index.graph, sym_id)
            blast_radii[sym_id] = radius
            all_blast.extend(radius)

    # Forward dependency expansion
    deps = expand_dependencies(
        index.graph,
        changed_symbols + all_blast,
        max_depth=max_depth,
    )

    # Impact scoring
    scores = compute_impact_scores(
        index.graph,
        changed_symbols,
        blast_radii,
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
) -> ContextPackage:
    """
    Phase 3: Select symbols and compile into LLM context.

    Now passes the call graph, dropped-symbol list, and broken-file list to
    compile_context so the LLM receives a full meta-header explaining its own
    blind spots (dropped symbols, incomplete graph regions, etc.).
    """
    selected, dropped = select_context(
        index.symbols,
        impact.scores,
        impact.changed,
        max_tokens=max_tokens,
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