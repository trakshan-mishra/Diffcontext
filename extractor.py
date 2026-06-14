import ast
import os


def extract_functions(filename, repo_path):
    with open(filename, "r", encoding="utf-8", errors="ignore") as f:
        source = f.read()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    functions = {}

    relative_file = "./" + os.path.relpath(filename, repo_path)

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        function_id = f"{relative_file}:{node.name}"

        code = ast.get_source_segment(source, node)
        if code is None:
            continue

        functions[function_id] = {
            "file": filename,
            "code": code,
        }

    return functions