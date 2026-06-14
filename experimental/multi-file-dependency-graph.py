import ast

from repository_scanner import find_python_files


def build_repository_graph(repo_path):

    graph = {}

    files = find_python_files(repo_path)

    for filename in files:

        with open(filename, "r") as f:
            source = f.read()

        tree = ast.parse(source)

        for node in tree.body:

            if isinstance(
                node,
                ast.FunctionDef
            ):

                graph[node.name] = []

                for child in ast.walk(node):

                    if isinstance(
                        child,
                        ast.Call
                    ):

                        if isinstance(
                            child.func,
                            ast.Name
                        ):

                            graph[node.name].append(
                                child.func.id
                            )

    return graph


if __name__ == "__main__":

    graph = build_repository_graph(".")

    print(graph)