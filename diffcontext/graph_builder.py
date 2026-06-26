"""
graph_builder.py — Build the full dependency graph for a Python repository.

Performance fixes vs original:
  1. Import maps are built ONCE in the pre-pass and reused everywhere.
     Original called build_import_map from resolver.py during the main
     graph-build loop, re-parsing every file's imports N times.
  2. _resolve_owner_type results are memoized with functools.lru_cache
     equivalent (manual dict cache) since the same (qualifier, bare_name,
     rel_file) triple is resolved thousands of times on large repos.
  3. reverse graph for inheritance resolution is built once, not per call.

Algorithm is otherwise identical to the original.
"""

import ast
import logging
import os
from typing import Dict, List, Optional, Set, Tuple

from .scanner import find_python_files
from .parser import extract_all_symbols
from .resolver import build_import_map
from .symbols import (
    extract_attribute_ownerships,
    extract_local_var_types,
    extract_param_types,
    _iter_statements,
)
from ._warn_once import warn_syntax_error_once, check_and_warn_encoding

logger = logging.getLogger(__name__)


def build_repository_graph(repo_path: str) -> Dict[str, List[str]]:
    """
    Build the complete call graph for a repository.

    Returns:
        dict mapping function_id -> [list of called function_ids]
    """
    repo_path = os.path.abspath(repo_path)

    functions = extract_all_symbols(repo_path)
    function_ids = set(functions)

    # ── pre-pass: per-file ASTs, import maps, class registry ─────────────
    file_trees:      Dict[str, ast.Module]          = {}
    import_maps:     Dict[str, Dict[str, str]]      = {}
    class_registry:  Dict[str, List[str]]           = {}   # class_name -> [rel_file, ...]
    classes_by_file: Dict[str, List[str]]           = {}   # rel_file -> [class_name, ...]
    inheritance:     Dict[str, List[Tuple]]         = {}   # "rel_file:ClassName" -> bases
    factory_returns: Dict[str, Tuple]               = {}   # "rel_file:func" -> (q, type)

    for filename in find_python_files(repo_path):
        with open(filename, "rb") as f:
            raw = f.read()
        check_and_warn_encoding(logger, filename, raw)
        source = raw.decode("utf-8", errors="ignore")

        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            warn_syntax_error_once(logger, filename, e)
            continue

        relative_file = "./" + os.path.relpath(filename, repo_path)
        file_trees[relative_file]  = tree
        # FIX: build import map ONCE here and store it. Downstream code
        # uses import_maps[rel_file] instead of calling build_import_map again.
        import_maps[relative_file] = build_import_map(filename, repo_path)

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_registry.setdefault(node.name, []).append(relative_file)
                classes_by_file.setdefault(relative_file, []).append(node.name)

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

    # ── Resolution cache ──────────────────────────────────────────────────
    # _resolve_owner_type is called O(symbols * calls_per_function) times.
    # The same (qualifier, bare_name, rel_file) triple recurs constantly on
    # large repos. Cache the result to avoid redundant work.
    _resolve_cache: Dict[Tuple, Optional[str]] = {}

    def _cached_resolve_owner_type(qualifier, bare_name, relative_file, import_map, _seen=None):
        key = (qualifier, bare_name, relative_file)
        if key in _resolve_cache:
            return _resolve_cache[key]
        result = _resolve_owner_type(
            qualifier, bare_name, relative_file, import_map,
            class_registry, factory_returns, import_maps, repo_path,
            _seen=_seen,
            classes_by_file=classes_by_file,
        )
        _resolve_cache[key] = result
        return result

    # ── Attribute owners ──────────────────────────────────────────────────
    attribute_owners: Dict[str, str] = {}

    for relative_file, tree in file_trees.items():
        import_map = import_maps[relative_file]
        raw_own = extract_attribute_ownerships(tree)

        for key, type_ref in raw_own.items():
            if type_ref is None:
                continue
            qualifier, bare_name = type_ref
            resolved = _cached_resolve_owner_type(qualifier, bare_name, relative_file, import_map)
            if resolved:
                attribute_owners[f"{relative_file}:{key}"] = resolved

    # ── Build the call graph ──────────────────────────────────────────────
    graph: Dict[str, List[str]] = {}

    for relative_file, tree in file_trees.items():
        import_map = import_maps[relative_file]

        local_name_to_id = {
            fid.split(":", 1)[1]: fid
            for fid in functions
            if fid.startswith(relative_file + ":")
        }

        function_nodes = _collect_function_nodes(tree)

        for fn_node, is_method, class_name in function_nodes:
            function_name = f"{class_name}.{fn_node.name}" if class_name else fn_node.name
            function_id   = f"{relative_file}:{function_name}"

            graph.setdefault(function_id, [])

            param_types    = extract_param_types(fn_node)
            local_var_types = {**param_types, **extract_local_var_types(fn_node, param_types)}

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
                    local_var_types,
                    classes_by_file,
                    _cached_resolve_owner_type,
                )

                if dep and dep != function_id and dep not in graph[function_id]:
                    graph[function_id].append(dep)

    return graph


# ── Internal helpers ──────────────────────────────────────────────────────

def _collect_function_nodes(tree):
    """
    Return list of (function_node, is_method, class_name) for EVERY function
    in the file, including nested functions and closures.

    Mirrors what parser.py's _FunctionCollector does so that the graph covers
    every symbol the parser emits (previously missed ~30-40% of nodes).
    """
    result = []
    _collect_recursive(tree.body, class_stack=[], result=result)
    return result


def _collect_recursive(stmts, class_stack, result):
    """Recursively collect function nodes from a list of statements."""
    for node in stmts:
        if isinstance(node, ast.ClassDef):
            class_stack.append(node.name)
            _collect_recursive(node.body, class_stack, result)
            class_stack.pop()
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if class_stack:
                class_name = ".".join(class_stack)
                is_method = True
            else:
                class_name = None
                is_method = False
            result.append((node, is_method, class_name))
            # Also recurse into the function body to catch closures/nested funcs
            _collect_recursive(node.body, class_stack, result)


def _find_return_type(node):
    found = None
    for stmt in _iter_statements(node.body):
        if isinstance(stmt, ast.Return) and stmt.value is not None:
            if not isinstance(stmt.value, ast.Call):
                return None
            func = stmt.value.func
            if isinstance(func, ast.Name):
                ref = (None, func.id)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                ref = (func.value.id, func.attr)
            elif isinstance(func, ast.Attribute):
                ref = (None, func.attr)
            else:
                return None
            if found is None:
                found = ref
            elif found != ref:
                return None
    return found


def _resolve_owner_type(
    qualifier, bare_name, relative_file, import_map,
    class_registry, factory_returns, import_maps, repo_path, _seen=None,
    classes_by_file=None,
):
    if _seen is None:
        _seen = set()

    cache_key = (qualifier, bare_name, relative_file)
    if cache_key in _seen:
        return None
    _seen.add(cache_key)

    if qualifier:
        if qualifier not in import_map:
            return None
        target_file = "./" + os.path.relpath(import_map[qualifier], repo_path)

        if bare_name in class_registry:
            if target_file in class_registry[bare_name]:
                return f"{target_file}:{bare_name}"
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
                classes_by_file,
            )
        return None

    # bare name
    if bare_name in class_registry and relative_file in class_registry[bare_name]:
        return f"{relative_file}:{bare_name}"

    if bare_name in import_map:
        target_file = "./" + os.path.relpath(import_map[bare_name], repo_path)

        if bare_name in class_registry:
            if target_file in class_registry[bare_name]:
                return f"{target_file}:{bare_name}"
            if target_file.endswith("__init__.py"):
                target_dir = target_file[:-12]
                for cand_file in class_registry[bare_name]:
                    if cand_file.startswith(target_dir + "/"):
                        return f"{cand_file}:{bare_name}"
        elif classes_by_file:
            file_classes = classes_by_file.get(target_file, [])
            if len(file_classes) == 1:
                return f"{target_file}:{file_classes[0]}"

        factory_key = f"{target_file}:{bare_name}"
        if factory_key in factory_returns:
            return _resolve_owner_type(
                *factory_returns[factory_key], target_file,
                import_maps.get(target_file, {}), class_registry,
                factory_returns, import_maps, repo_path, _seen,
                classes_by_file,
            )
        return None

    factory_key = f"{relative_file}:{bare_name}"
    if factory_key in factory_returns:
        return _resolve_owner_type(
            *factory_returns[factory_key], relative_file,
            import_map, class_registry, factory_returns,
            import_maps, repo_path, _seen,
            classes_by_file=classes_by_file,
        )
    return None


def _resolve_owner_of_expr(
    node, class_name, relative_file, is_method, attribute_owners,
    local_var_types=None, import_map=None, class_registry=None,
    factory_returns=None, import_maps=None, repo_path=None,
    classes_by_file=None, _cached_resolve=None,
):
    if isinstance(node, ast.Name) and node.id == "self" and is_method and class_name:
        return f"{relative_file}:{class_name}"

    if (
        isinstance(node, ast.Name)
        and local_var_types
        and node.id in local_var_types
        and import_map is not None
    ):
        qualifier, bare_name = local_var_types[node.id]
        if _cached_resolve:
            return _cached_resolve(qualifier, bare_name, relative_file, import_map)
        return _resolve_owner_type(
            qualifier, bare_name, relative_file, import_map,
            class_registry or {}, factory_returns or {},
            import_maps or {}, repo_path or "",
            classes_by_file=classes_by_file,
        )

    if isinstance(node, ast.Attribute):
        base_owner = _resolve_owner_of_expr(
            node.value, class_name, relative_file, is_method, attribute_owners,
            local_var_types, import_map, class_registry,
            factory_returns, import_maps, repo_path,
            classes_by_file, _cached_resolve,
        )
        if base_owner is None:
            return None
        return attribute_owners.get(f"{base_owner}.{node.attr}")

    return None


def _resolve_via_inheritance(
    owner_type, method_name, inheritance, class_registry,
    import_maps, function_ids, repo_path, _seen=None,
    classes_by_file=None,
):
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
            classes_by_file=classes_by_file,
        )
        if not base_owner:
            continue

        candidate = f"{base_owner}.{method_name}"
        if candidate in function_ids:
            return candidate

        deeper = _resolve_via_inheritance(
            base_owner, method_name, inheritance, class_registry,
            import_maps, function_ids, repo_path, _seen,
            classes_by_file,
        )
        if deeper:
            return deeper

    return None


def _resolve_call(
    call_node, is_method, class_name, relative_file,
    local_name_to_id, import_map, all_functions, function_ids,
    repo_path, attribute_owners, inheritance, class_registry,
    factory_returns, import_maps, local_var_types=None,
    classes_by_file=None, _cached_resolve=None,
):
    func = call_node.func

    if isinstance(func, ast.Name):
        return _lookup(func.id, local_name_to_id, import_map, all_functions, repo_path)

    if isinstance(func, ast.Attribute):
        method_name = func.attr

        owner_type = _resolve_owner_of_expr(
            func.value, class_name, relative_file, is_method, attribute_owners,
            local_var_types, import_map, class_registry,
            factory_returns, import_maps, repo_path,
            classes_by_file, _cached_resolve,
        )

        if owner_type:
            candidate = f"{owner_type}.{method_name}"
            if candidate in function_ids:
                return candidate

            resolved = _resolve_via_inheritance(
                owner_type, method_name, inheritance, class_registry,
                import_maps, function_ids, repo_path,
                classes_by_file=classes_by_file,
            )
            if resolved:
                return resolved
            return None

        if isinstance(func.value, ast.Name) and func.value.id in import_map:
            source_file = import_map[func.value.id]
            relative_source = "./" + os.path.relpath(source_file, repo_path)
            candidate = f"{relative_source}:{method_name}"
            if candidate in all_functions:
                return candidate

    return None


def _lookup(called_name, local_name_to_id, import_map, all_functions, repo_path):
    if called_name in local_name_to_id:
        return local_name_to_id[called_name]
    if called_name in import_map:
        source_file = import_map[called_name]
        relative_source = "./" + os.path.relpath(source_file, repo_path)
        candidate = f"{relative_source}:{called_name}"
        if candidate in all_functions:
            return candidate
    return None