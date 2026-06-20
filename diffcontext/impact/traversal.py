"""
traversal.py — Dependency expansion (forward graph walk).

Given selected symbols, walk forward edges to include their callees.
Supports both unbounded DFS and bounded BFS.
"""

from typing import Dict, List, Optional, Set


def expand_dependencies(
    graph: Dict[str, List[str]],
    selected_symbols: List[str],
    max_depth: Optional[int] = None,
) -> List[str]:
    """
    Walk forward edges from selected_symbols.

    max_depth=None  -> full transitive closure (iterative DFS)
    max_depth=N     -> only nodes reachable within N hops (BFS)
    """
    visited: Set[str] = set()
    result: List[str] = []

    if max_depth is None:
        # Iterative DFS — safe on large repos (no recursion limit)
        stack = list(selected_symbols)
        while stack:
            func = stack.pop()
            if func in visited:
                continue
            visited.add(func)
            result.append(func)
            for dep in reversed(graph.get(func, [])):
                if dep not in visited:
                    stack.append(dep)
        return result

    # Bounded BFS
    frontier = list(selected_symbols)
    for func in frontier:
        if func not in visited:
            visited.add(func)
            result.append(func)

    depth = 0
    while frontier and depth < max_depth:
        next_frontier = []
        for func in frontier:
            for dep in graph.get(func, []):
                if dep not in visited:
                    visited.add(dep)
                    result.append(dep)
                    next_frontier.append(dep)
        frontier = next_frontier
        depth += 1

    return result
