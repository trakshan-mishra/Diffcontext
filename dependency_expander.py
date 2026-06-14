from multi_file_dependency_graph import (
    build_repository_graph
)


def expand_dependencies(graph, selected_functions):

    visited = set()
    result = []

    def dfs(func):

        if func in visited:
            return

        visited.add(func)
        result.append(func)

        for dep in graph.get(func, []):
            dfs(dep)

    for func in selected_functions:
        dfs(func)

    return result


if __name__ == "__main__":

    graph = build_repository_graph(".")

    selected = [
        "./app.py:report"
    ]

    print(
        expand_dependencies(
            graph,
            selected
        )
    )