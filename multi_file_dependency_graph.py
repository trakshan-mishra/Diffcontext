import ast
import os

from repo_scanner import find_python_files
from repo_extractor import extract_repository_functions


def build_repository_graph(repo_path):

    graph = {}

    functions = extract_repository_functions(
        repo_path
    )

    files = find_python_files(repo_path)

    for filename in files:

        with open(filename, "r") as f:
            source = f.read()

        tree = ast.parse(source)

        relative_file = (
            "./" + os.path.basename(filename)
        )

        for node in tree.body:

            if not isinstance(
                node,
                ast.FunctionDef
            ):
                continue

            function_id = (
                f"{relative_file}:{node.name}"
            )

            graph[function_id] = []

            for child in ast.walk(node):

                if not isinstance(
                    child,
                    ast.Call
                ):
                    continue

                if not isinstance(
                    child.func,
                    ast.Name
                ):
                    continue

                called_name = child.func.id

                local_function = (
                    f"{relative_file}:{called_name}"
                )

                if local_function in functions:

                    graph[function_id].append(
                        local_function
                    )

    return graph


if __name__ == "__main__":

    graph = build_repository_graph(".")

    for function, deps in graph.items():
        print(function, "->", deps)