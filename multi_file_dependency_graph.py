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

        relative_file = "./" + os.path.relpath(
            filename,
            repo_path
        )

        local_name_to_id = {
            fid.split(":", 1)[1]: fid
            for fid in functions
            if fid.startswith(relative_file + ":")
        }

        import_map = build_import_map(
            filename,
            repo_path
        )

        function_nodes = _collect_function_nodes(tree)

        for fn_node, is_method, class_name in function_nodes:

            if class_name:
                function_name = (
                    f"{class_name}.{fn_node.name}"
                )
            else:
                function_name = fn_node.name

            function_id = (
                f"{relative_file}:{function_name}"
            )

            graph.setdefault(function_id, [])

            for child in ast.walk(fn_node):

                if not isinstance(child, ast.Call):
                    continue

                dep = _resolve_call(
                    child,
                    is_method,
                    class_name,
                    local_name_to_id,
                    import_map,
                    functions,
                    repo_path,
                )

                if (
                    dep
                    and dep != function_id
                    and dep not in graph[function_id]
                ):
                    graph[function_id].append(dep)

    return graph


def _collect_function_nodes(tree):
    """
    Returns:
        (function_node, is_method, class_name)
    """

    result = []

    for node in tree.body:

        if isinstance(node, ast.FunctionDef):
            result.append(
                (
                    node,
                    False,
                    None,
                )
            )

        elif isinstance(node, ast.AsyncFunctionDef):
            result.append(
                (
                    node,
                    False,
                    None,
                )
            )

        elif isinstance(node, ast.ClassDef):

            for item in node.body:

                if isinstance(
                    item,
                    (
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                    ),
                ):
                    result.append(
                        (
                            item,
                            True,
                            node.name,
                        )
                    )

    return result


def _resolve_call(
    call_node,
    is_method,
    class_name,
    local_name_to_id,
    import_map,
    all_functions,
    repo_path,
):
    """
    Handles:

    helper()

    self.method()

    module.function()
    """

    func = call_node.func

    # -------------------------
    # helper()
    # -------------------------
    if isinstance(func, ast.Name):

        called_name = func.id

        return _lookup(
            called_name,
            local_name_to_id,
            import_map,
            all_functions,
            repo_path,
        )

    # -------------------------
    # self.method()
    # module.function()
    # obj.method()
    # -------------------------
    if isinstance(func, ast.Attribute):

        method_name = func.attr

        obj = func.value

        # self.method()
        if (
            isinstance(obj, ast.Name)
            and obj.id == "self"
            and is_method
            and class_name
        ):

            owned_name = (
                f"{class_name}.{method_name}"
            )

            if owned_name in local_name_to_id:
                return local_name_to_id[
                    owned_name
                ]

        # module.function()
        if (
            isinstance(obj, ast.Name)
            and obj.id in import_map
        ):

            source_file = import_map[obj.id]

            relative_source = (
                "./"
                + os.path.relpath(
                    source_file,
                    repo_path,
                )
            )

            candidate = (
                f"{relative_source}:{method_name}"
            )

            if candidate in all_functions:
                return candidate

    return None


def _lookup(
    called_name,
    local_name_to_id,
    import_map,
    all_functions,
    repo_path,
):
    """
    helper()

    imported_function()
    """

    if called_name in local_name_to_id:
        return local_name_to_id[
            called_name
        ]

    if called_name in import_map:

        source_file = import_map[
            called_name
        ]

        relative_source = (
            "./"
            + os.path.relpath(
                source_file,
                repo_path,
            )
        )

        candidate = (
            f"{relative_source}:{called_name}"
        )

        if candidate in all_functions:
            return candidate

    return None


if __name__ == "__main__":

    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "."
    )

    graph = build_repository_graph(path)

    edges = 0

    for fn, deps in sorted(graph.items()):

        if deps:

            print(fn)

            for dep in deps:
                print(f"  -> {dep}")

            edges += len(deps)

    print(
        f"\nNodes: {len(graph)}  Edges: {edges}"
    )