import ast
import os
import sys

from repo_scanner import find_python_files
from repo_extractor import extract_repository_functions
from import_resolver import build_import_map
from attribute_ownership import (
    extract_attribute_ownerships,
    _iter_statements,
)


def build_repository_graph(repo_path):
    repo_path = os.path.abspath(repo_path)

    functions = extract_repository_functions(repo_path)
    function_ids = set(functions)

    # ---- pre-pass: per-file ASTs, import maps, class registry,
    #      inheritance, factory return types -----------------------------
    file_trees = {}
    import_maps = {}
    class_registry = {}    # class_name -> [relative_file, ...]
    inheritance = {}       # "rel_file:ClassName" -> [(qualifier, base_name), ...]
    factory_returns = {}   # "rel_file:func_name" -> (qualifier, type_name)

    for filename in find_python_files(repo_path):

        with open(filename, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()

        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        relative_file = "./" + os.path.relpath(filename, repo_path)

        file_trees[relative_file] = tree
        import_maps[relative_file] = build_import_map(filename, repo_path)

        for node in tree.body:

            if isinstance(node, ast.ClassDef):
                class_registry.setdefault(node.name, []).append(relative_file)

                bases = []
                for b in node.bases:
                    if isinstance(b, ast.Name):
                        bases.append((None, b.id))
                    elif isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name):
                        bases.append((b.value.id, b.attr))
                    elif isinstance(b, ast.Attribute):
                        bases.append((None, b.attr))

                inheritance[f"{relative_file}:{node.name}"] = bases

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ref = _find_return_type(node)
                if ref:
                    factory_returns[f"{relative_file}:{node.name}"] = ref

    # ---- attribute owners, fully resolved up front ------------------------
    # key:   "rel_file:ClassName.attr"
    # value: "owner_rel_file:OwnerClassName"   (== a fid prefix)
    attribute_owners = {}

    for relative_file, tree in file_trees.items():
        import_map = import_maps[relative_file]
        raw = extract_attribute_ownerships(tree)

        for key, type_ref in raw.items():
            if type_ref is None:
                continue
            qualifier, bare_name = type_ref
            resolved = _resolve_owner_type(
                qualifier, bare_name, relative_file, import_map,
                class_registry, factory_returns, import_maps, repo_path,
            )
            if resolved:
                attribute_owners[f"{relative_file}:{key}"] = resolved

    # ---- build the call graph ---------------------------------------------
    graph = {}

    for relative_file, tree in file_trees.items():

        import_map = import_maps[relative_file]

        local_name_to_id = {
            fid.split(":", 1)[1]: fid
            for fid in functions
            if fid.startswith(relative_file + ":")
        }

        function_nodes = _collect_function_nodes(tree)

        for fn_node, is_method, class_name in function_nodes:

            if class_name:
                function_name = f"{class_name}.{fn_node.name}"
            else:
                function_name = fn_node.name

            function_id = f"{relative_file}:{function_name}"

            graph.setdefault(function_id, [])

            for child in ast.walk(fn_node):

                if not isinstance(child, ast.Call):
                    continue

                dep = _resolve_call(
                    child,
                    is_method,
                    class_name,
                    relative_file,
                    local_name_to_id,
                    import_map,
                    functions,
                    function_ids,
                    repo_path,
                    attribute_owners,
                    inheritance,
                    class_registry,
                    factory_returns,
                    import_maps,
                )

                if (
                    dep
                    and dep != function_id
                    and dep not in graph[function_id]
                ):
                    graph[function_id].append(dep)

    return graph


def _collect_function_nodes(tree):
    """Returns (function_node, is_method, class_name)."""

    result = []

    for node in tree.body:

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.append((node, False, None))

        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result.append((item, True, node.name))

    return result


def _find_return_type(node):
    """
    Best-effort return-type inference for simple factory functions:

        def make_helper():
            return Helper()

        def make_router():
            return routing.Router()

    Returns (qualifier, type_name) if every `return <Call>` in the function
    constructs the same type, else None.
    """
    found = None

    for stmt in _iter_statements(node.body):

        if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Call):
            f = stmt.value.func

            if isinstance(f, ast.Name):
                ref = (None, f.id)
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                ref = (f.value.id, f.attr)
            elif isinstance(f, ast.Attribute):
                ref = (None, f.attr)
            else:
                continue

            if found is None:
                found = ref
            elif found != ref:
                return None  # ambiguous - different return types

    return found


def _resolve_owner_type(
    qualifier, bare_name, relative_file, import_map,
    class_registry, factory_returns, import_maps, repo_path, _seen=None,
):
    """
    Resolve a (qualifier, bare_name) type reference -- as written in the
    file `relative_file` -- to "owner_rel_file:ClassName" (a fid prefix).

    File-scoped: only resolves through THIS file's own imports / locally
    defined classes, never a global namespace. This is what prevents
    cross-file name collisions (two different "Router" classes) from
    being silently confused with each other.

    Also chases through one-level factory functions:
        self.helper = make_helper()   where make_helper() -> Helper()
    """
    if _seen is None:
        _seen = set()

    cache_key = (qualifier, bare_name, relative_file)
    if cache_key in _seen:
        return None
    _seen.add(cache_key)

    if qualifier:
        # routing.Router -> "routing" must be an imported module in this file
        if qualifier not in import_map:
            return None

        target_file = "./" + os.path.relpath(import_map[qualifier], repo_path)

        if bare_name in class_registry:
            if target_file in class_registry[bare_name]:
                return f"{target_file}:{bare_name}"
            # Sub-package scanning: if target is an __init__.py, look inside its directory
            if target_file.endswith("__init__.py"):
                target_dir = target_file[:-12]  # strip /__init__.py
                for cand_file in class_registry[bare_name]:
                    if cand_file.startswith(target_dir + "/"):
                        return f"{cand_file}:{bare_name}"

        factory_key = f"{target_file}:{bare_name}"
        if factory_key in factory_returns:
            return _resolve_owner_type(
                *factory_returns[factory_key], target_file,
                import_maps.get(target_file, {}), class_registry,
                factory_returns, import_maps, repo_path, _seen,
            )

        return None

    # ---- no qualifier: bare name -----------------------------------------

    # locally defined class in this same file
    if bare_name in class_registry and relative_file in class_registry[bare_name]:
        return f"{relative_file}:{bare_name}"

    # imported directly: `from routing import Router`
    if bare_name in import_map:
        target_file = "./" + os.path.relpath(import_map[bare_name], repo_path)

        if bare_name in class_registry:
            if target_file in class_registry[bare_name]:
                return f"{target_file}:{bare_name}"
            # Sub-package scanning
            if target_file.endswith("__init__.py"):
                target_dir = target_file[:-12]
                for cand_file in class_registry[bare_name]:
                    if cand_file.startswith(target_dir + "/"):
                        return f"{cand_file}:{bare_name}"

        factory_key = f"{target_file}:{bare_name}"
        if factory_key in factory_returns:
            return _resolve_owner_type(
                *factory_returns[factory_key], target_file,
                import_maps.get(target_file, {}), class_registry,
                factory_returns, import_maps, repo_path, _seen,
            )

        return None

    # local factory function: `def make_helper(): return Helper()`
    factory_key = f"{relative_file}:{bare_name}"
    if factory_key in factory_returns:
        return _resolve_owner_type(
            *factory_returns[factory_key], relative_file,
            import_map, class_registry, factory_returns,
            import_maps, repo_path, _seen,
        )

    return None


def _resolve_owner_of_expr(node, class_name, relative_file, is_method, attribute_owners):
    """
    For the receiver expression of a method call (everything before the
    final `.method`), return its type as "rel_file:ClassName", or None.

    Handles arbitrary-depth attribute chains:
        self                -> rel_file:ClassName
        self.router         -> attribute_owners["rel_file:ClassName.router"]
        self.state.router   -> chase attribute_owners twice
    """
    if isinstance(node, ast.Name) and node.id == "self" and is_method and class_name:
        return f"{relative_file}:{class_name}"

    if isinstance(node, ast.Attribute):
        base_owner = _resolve_owner_of_expr(
            node.value, class_name, relative_file, is_method, attribute_owners
        )
        if base_owner is None:
            return None
        return attribute_owners.get(f"{base_owner}.{node.attr}")

    return None


def _resolve_via_inheritance(
    owner_type, method_name, inheritance, class_registry,
    import_maps, function_ids, repo_path, _seen=None,
):
    """
    owner_type defines `method_name`? No -> walk its base classes
    (possibly in other files) and try them too.
    """
    if _seen is None:
        _seen = set()
    if owner_type in _seen:
        return None
    _seen.add(owner_type)

    rel_file, class_name = owner_type.split(":", 1)
    import_map = import_maps.get(rel_file, {})

    for qualifier, base_name in inheritance.get(owner_type, []):
        base_owner = _resolve_owner_type(
            qualifier, base_name, rel_file, import_map,
            class_registry, {}, import_maps, repo_path,
        )
        if not base_owner:
            continue

        candidate = f"{base_owner}.{method_name}"
        if candidate in function_ids:
            return candidate

        deeper = _resolve_via_inheritance(
            base_owner, method_name, inheritance, class_registry,
            import_maps, function_ids, repo_path, _seen,
        )
        if deeper:
            return deeper

    return None


def _resolve_call(
    call_node,
    is_method,
    class_name,
    relative_file,
    local_name_to_id,
    import_map,
    all_functions,
    function_ids,
    repo_path,
    attribute_owners,
    inheritance,
    class_registry,
    factory_returns,
    import_maps,
):
    """
    Handles:
        helper()
        self.method()                 (incl. inherited, possibly cross-file)
        self.attr.method()
        self.a.b.method()              (multi-hop attribute chains)
        module.function()
    """

    func = call_node.func

    # -------------------------
    # helper()
    # -------------------------
    if isinstance(func, ast.Name):
        return _lookup(func.id, local_name_to_id, import_map, all_functions, repo_path)

    if isinstance(func, ast.Attribute):
        method_name = func.attr

        # self / self.attr / self.a.b ... -> resolve receiver's type
        owner_type = _resolve_owner_of_expr(
            func.value, class_name, relative_file, is_method, attribute_owners
        )

        if owner_type:
            candidate = f"{owner_type}.{method_name}"

            if candidate in function_ids:
                return candidate

            resolved = _resolve_via_inheritance(
                owner_type, method_name, inheritance, class_registry,
                import_maps, function_ids, repo_path,
            )
            if resolved:
                return resolved

            return None

        # ----------------------------------
        # module.function()
        # ----------------------------------
        if isinstance(func.value, ast.Name) and func.value.id in import_map:
            source_file = import_map[func.value.id]
            relative_source = "./" + os.path.relpath(source_file, repo_path)
            candidate = f"{relative_source}:{method_name}"

            if candidate in all_functions:
                return candidate

    return None


def _lookup(called_name, local_name_to_id, import_map, all_functions, repo_path):
    """helper() / imported_function()"""

    if called_name in local_name_to_id:
        return local_name_to_id[called_name]

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
        print(fn)
        for dep in deps:
            print(f"  -> {dep}")
        edges += len(deps)

    print(f"\nNodes: {len(graph)}  Edges: {edges}")
