"""
benchmarks/evaluator.py — Core DiffContext pipeline runner for benchmarks.
"""

import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.parser import extract_all_symbols
from diffcontext.graph_builder import build_repository_graph
from diffcontext.impact.blast_radius import get_blast_radius
from diffcontext.impact.traversal import expand_dependencies


def run_diffcontext(
    repo_path: str,
    changed_functions: List[str],
    max_depth: Optional[int] = 2,
) -> List[str]:
    """
    Run the full DiffContext pipeline on a repo given changed function IDs.

    Returns list of function IDs that should be included in context.
    """
    graph = build_repository_graph(repo_path)

    # Start with changed functions
    selected = set(changed_functions)

    # Add blast radius (functions that call the changed functions)
    for func in changed_functions:
        if func in graph:
            selected.update(get_blast_radius(graph, func))

    # Expand dependencies with depth limit (what changed functions call)
    expanded = expand_dependencies(graph, list(selected), max_depth=max_depth)

    return expanded
