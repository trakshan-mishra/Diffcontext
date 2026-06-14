"""
dependency_expander.py  — iterative rewrite (bug fix)
=======================================================
BUG in original: recursive DFS hits Python's default recursion limit (~1000)
on real repos with deep call chains (e.g. fastapi routing → starlette → ...).

FIX: convert unbounded DFS to iterative stack-based DFS.
Bounded BFS path was already iterative; no change needed there.

API is identical to original — drop-in replacement.
"""

from multi_file_dependency_graph import build_repository_graph


def expand_dependencies(graph, selected_functions, max_depth=None):
    """
    Walk forward edges from selected_functions.

    max_depth=None  → full transitive closure (iterative DFS, no recursion limit)
    max_depth=N     → only nodes reachable within N hops (BFS)
    """

    visited = set()
    result = []

    if max_depth is None:
        # Iterative DFS — identical semantics to recursive version,
        # but safe on large repos.
        stack = list(selected_functions)
        while stack:
            func = stack.pop()
            if func in visited:
                continue
            visited.add(func)
            result.append(func)
            # push deps in reverse so left-most dep is processed first
            for dep in reversed(graph.get(func, [])):
                if dep not in visited:
                    stack.append(dep)
        return result

    # Bounded BFS (unchanged from original)
    frontier = list(selected_functions)
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


if __name__ == "__main__":
    graph = build_repository_graph(".")
    selected = ["./app.py:report"]
    print(expand_dependencies(graph, selected))
    print(expand_dependencies(graph, selected, max_depth=1))
