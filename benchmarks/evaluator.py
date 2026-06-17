"""
evaluator.py - Core DiffContext pipeline with precision improvements.
"""

from repo_extractor import extract_repository_functions
from multi_file_dependency_graph import build_repository_graph
from blast_radius import get_blast_radius
from dependency_expander import expand_dependencies


def run_diffcontext(repo_path, changed_functions, max_depth=2):
    """
    Run the full DiffContext pipeline on a repo given a list of changed function IDs.

    Args:
        repo_path: path to the repository root
        changed_functions: list of function IDs e.g. ["./api.py:get"]
        max_depth: maximum dependency depth (1=direct, 2=one hop, None=full)

    Returns:
        list of function IDs that should be included in context
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


