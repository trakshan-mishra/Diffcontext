"""
pipeline.py — The main DiffContext pipeline.

Connects all stages: parse -> graph -> diff -> blast radius -> score -> select -> compile
"""

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


def index_repository(repo_path: str) -> RepositoryIndex:
    """
    Phase 1: Parse repository and build dependency graph.

    Returns a RepositoryIndex with all symbols and the call graph.
    """
    repo_path = os.path.abspath(repo_path)

    symbols = extract_all_symbols(repo_path)
    graph = build_repository_graph(repo_path)

    return RepositoryIndex(symbols=symbols, graph=graph)


def analyze_impact(
    index: RepositoryIndex,
    changed_symbols: List[str],
    max_depth: Optional[int] = 2,
) -> ImpactResult:
    """
    Phase 2: Given changed symbols, compute blast radius and impact scores.
    """
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
) -> ContextPackage:
    """
    Phase 3: Select symbols and compile into LLM context.
    """
    selected = select_context(
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
