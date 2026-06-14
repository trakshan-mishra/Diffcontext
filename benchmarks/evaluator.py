from repo_extractor import extract_repository_functions
from multi_file_dependency_graph import build_repository_graph
from blast_radius import get_blast_radius
from dependency_expander import expand_dependencies


def run_diffcontext(repo_path, changed_functions):
    """
    Run the full DiffContext pipeline on a repo given a list of changed function IDs.

    Args:
        repo_path: path to the repository root
        changed_functions: list of function IDs e.g. ["./api.py:get"]

    Returns:
        list of function IDs that should be included in context
    """
    graph = build_repository_graph(repo_path)

    selected = set(changed_functions)

    for func in changed_functions:
        selected.update(get_blast_radius(graph, func))

    expanded = expand_dependencies(graph, list(selected))

    return expanded