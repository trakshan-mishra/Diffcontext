from multi_file_dependency_graph import (
    build_repository_graph
)


def get_blast_radius(graph, changed_function):

    affected = set()

    def dfs(target):

        for function, dependencies in graph.items():

            if target in dependencies:

                if function not in affected:

                    affected.add(function)

                    dfs(function)

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