import ast


def build_dependency_graph(filename):

    with open(filename, "r") as f:
        source = f.read()

    tree = ast.parse(source)

    graph = {}

    for node in tree.body:

        if isinstance(node, ast.FunctionDef):

            function_name = node.name

            graph[function_name] = []

            for child in ast.walk(node):

                if isinstance(child, ast.Call):

                    if isinstance(child.func, ast.Name):

                        graph[function_name].append(
                            child.func.id
                        )

    return graph


if __name__ == "__main__":

    graph = build_dependency_graph("app.py")

    print(graph)