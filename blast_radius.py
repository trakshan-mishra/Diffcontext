from multi_file_dependency_graph import (
    build_repository_graph
)


def get_blast_radius(graph, changed_function):
    """
    All functions that (transitively) call changed_function.
    Now with cycle detection.
    """
    reverse = {}
    for function, dependencies in graph.items():
        for dep in dependencies:
            reverse.setdefault(dep, set()).add(function)

    affected = set()
    visited = set()  # ADD THIS

    def dfs(target):
        if target in visited:  # ADD THIS - cycle detection
            return
        visited.add(target)    # ADD THIS
        
        for caller in reverse.get(target, ()):
            if caller not in affected:
                affected.add(caller)
                dfs(caller)

    dfs(changed_function)
    return list(affected)


if __name__ == "__main__":

    graph = build_repository_graph(".")

    print(graph)

    print(
        get_blast_radius(
            graph,
            "./app.py:add"
        )
    )
