from multi_file_dependency_graph import (
    build_repository_graph
)


def get_blast_radius(graph, changed_function):
    """
    All functions that (transitively) call changed_function.

    Old version re-scanned graph.items() for every node visited during
    the DFS -> O(V*E). Here we build the reverse adjacency once, then
    DFS is O(V+E).
    """

    reverse = {}
    for function, dependencies in graph.items():
        for dep in dependencies:
            reverse.setdefault(dep, set()).add(function)

    affected = set()

    def dfs(target):
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
