"""
benchmarks/evaluator.py — Core DiffContext pipeline runner for benchmarks.

Fixes vs original:
  - Reverse graph built once and reused across blast radius calls.
  - expanded_deps passed to compute_impact_scores (was None before).
  - max_depth raised to 3 to catch deeper co-change relationships.
"""

import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import get_blast_radius, build_reverse_graph
from diffcontext.impact.traversal import expand_dependencies
from diffcontext.impact.scoring import compute_impact_scores
from diffcontext.context.selector import select_context


def run_diffcontext(
    repo_path: str,
    changed_functions: List[str],
    max_depth: Optional[int] = 3,
) -> List[str]:
    """
    Run the full DiffContext pipeline on a repo given changed function IDs.

    Returns list of function IDs that should be included in context.
    """
    graph = build_repository_graph(repo_path)
    symbols = extract_all_symbols(repo_path)

    # Build reverse graph ONCE — reused by blast radius and scoring
    reverse = build_reverse_graph(graph)

    # Blast radii for all changed functions
    blast_radii = {
        func: get_blast_radius(graph, func, reverse=reverse)
        for func in changed_functions
        if func in graph
    }
    all_blast = [sym for r in blast_radii.values() for sym in r]

    seed = list(set(changed_functions) | set(all_blast))
    expanded = expand_dependencies(graph, seed, max_depth=max_depth)

    scores = compute_impact_scores(
        graph, changed_functions, blast_radii,
        expanded_deps=expanded,
        reverse=reverse,
    )

    selected, _ = select_context(symbols, scores, changed_functions)
    return selected
