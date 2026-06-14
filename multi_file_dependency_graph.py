import ast
import os
import sys

from repo_scanner import find_python_files
from repo_extractor import extract_repository_functions
from import_resolver import build_import_map


def build_repository_graph(repo_path):
    repo_path = os.path.abspath(repo_path)
    graph = {}

    functions = extract_repository_functions(repo_path)

    for filename in find_python_files(repo_path):
        with open(filename, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()

        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        relative_file = "./" + os.path.relpath(filename, repo_path)

        # All function IDs defined in THIS file
        local_name_to_id = {
            fid.split(":", 1)[1]: fid
            for fid in functions
            if fid.startswith(relative_file + ":")
        }

        import_map = build_import_map(filename, repo_path)

        # Collect (function_node, is_method) from top-level and class bodies
        function_nodes = _collect_function_nodes(tree)

        for fn_node, is_method in function_nodes:
            function_id = f"{relative_file}:{fn_node.name}"
            graph[function_id] = []

            for child in ast.walk(fn_node):
                if not isinstance(child, ast.Call):
                    continue

                dep = _resolve_call(
                    child,
                    is_method,
                    local_name_to_id,
                    import_map,
                    functions,
                    repo_path,
                )

                if dep and dep != function_id and dep not in graph[function_id]:
                    graph[function_id].append(dep)

    return graph


def _collect_function_nodes(tree):
    """
    Yield (FunctionDef_node, is_method) for all functions in the file,
    including methods inside class bodies.
    """
    result = []

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            result.append((node, False))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    result.append((item, True))

    return result


def _resolve_call(call_node, is_method, local_name_to_id, import_map, all_functions, repo_path):
    """
    Resolve a Call node to a function_id in the repository.

    Handles:
      - bare calls:       helper()            → local or imported
      - self calls:       self.method()       → same-file method
      - attribute calls:  module.function()   → imported module function
    """
    func = call_node.func

    # Case 1: bare call — helper()
    if isinstance(func, ast.Name):
        called_name = func.id
        return _lookup(called_name, local_name_to_id, import_map, all_functions, repo_path)

    # Case 2: attribute call — obj.method()
    if isinstance(func, ast.Attribute):
        method_name = func.attr
        obj = func.value

        # self.method() → find method_name in same file
        if isinstance(obj, ast.Name) and obj.id == "self" and is_method:
            if method_name in local_name_to_id:
                return local_name_to_id[method_name]

        # module.function() → check if obj is an imported name
        if isinstance(obj, ast.Name) and obj.id in import_map:
            source_file = import_map[obj.id]
            relative_source = "./" + os.path.relpath(source_file, repo_path)
            candidate = f"{relative_source}:{method_name}"
            if candidate in all_functions:
                return candidate

    return None


def _lookup(called_name, local_name_to_id, import_map, all_functions, repo_path):
    # Local function first
    if called_name in local_name_to_id:
        return local_name_to_id[called_name]

    # Imported function
    if called_name in import_map:
        source_file = import_map[called_name]
        relative_source = "./" + os.path.relpath(source_file, repo_path)
        candidate = f"{relative_source}:{called_name}"
        if candidate in all_functions:
            return candidate

    return None


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    graph = build_repository_graph(path)

    edges = 0
    for fn, deps in sorted(graph.items()):
        if deps:
            print(f"{fn}")
            for d in deps:
                print(f"  -> {d}")
            edges += len(deps)

    print(f"\nNodes: {len(graph)}  Edges: {edges}")