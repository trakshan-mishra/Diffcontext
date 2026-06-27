"""
graph_builder.py — Build the full dependency graph for a Python repository.

Edge types (v2):
  1. Direct call edges           — f() calls g()  →  f→g
  2. Inheritance override edges  — Child.method → Parent.method (child only)
  3. Shared-import consumer edges— files co-importing same module (capped ≤10)
  4. Decorator edges             — @decorator applied to a function  →  fn→decorator
  5. Annotated return-type edges — def f() -> MyClass: ...  →  f→MyClass.__init__
  6. Same-directory sibling edges— one representative per file, light connectivity

Performance:
  - Import maps built ONCE in pre-pass.
  - _resolve_owner_type results memoized (manual dict cache).
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

# Fan-out caps — keep shared-import and sibling edges from becoming mega-hubs
_SHARED_IMPORT_MAX_CONSUMERS = 10   # skip if >10 files share the same import
_SAME_DIR_MAX_FILES = 20            # skip same-dir bonus if directory is huge
_DECORATOR_EDGE_MAX = 6             # max decorator edges per function (guards chains)


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
    # Module-level var types: rel_file -> {var_name: (qualifier, bare_name)}
    # e.g. `app = Flask(__name__)` at module scope -> {"app": (None, "Flask")}
    module_var_types: Dict[str, Dict[str, Tuple]]   = {}

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
        import_maps[relative_file] = build_import_map(filename, repo_path)

        mvt: Dict[str, Tuple] = {}
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
                ann_ref = _find_annotated_return(node)
                if ann_ref and not ref:
                    factory_returns[f"{relative_file}:{node.name}"] = ann_ref

            elif isinstance(node, ast.Assign):
                # Track module-level: `app = Flask(__name__)`
                if isinstance(node.value, ast.Call):
                    func = node.value.func
                    if isinstance(func, ast.Name):
                        type_ref = (None, func.id)
                    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        type_ref = (func.value.id, func.attr)
                    else:
                        type_ref = None
                    if type_ref:
                        for tgt in node.targets:
                            if isinstance(tgt, ast.Name):
                                mvt[tgt.id] = type_ref

            elif isinstance(node, ast.AnnAssign):
                # Track: `db: SQLAlchemy = SQLAlchemy(app)`
                if node.value and isinstance(node.value, ast.Call):
                    func = node.value.func
                    if isinstance(func, ast.Name):
                        type_ref = (None, func.id)
                    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        type_ref = (func.value.id, func.attr)
                    else:
                        type_ref = None
                    if type_ref and isinstance(node.target, ast.Name):
                        mvt[node.target.id] = type_ref

        if mvt:
            module_var_types[relative_file] = mvt

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

    # Also register module-level vars as attribute owners so that
    # `app.run()` in a function body resolves to Flask.run.
    for rel_file, mvt in module_var_types.items():
        import_map = import_maps[rel_file]
        for var_name, type_ref in mvt.items():
            qualifier, bare_name = type_ref
            resolved = _resolve_owner_type(
                qualifier, bare_name, rel_file, import_map,
                class_registry, factory_returns, import_maps, repo_path,
                classes_by_file=classes_by_file,
            )
            if resolved:
                # Key format matches attribute_owners: "ClassName.attr"
                # but here var_name is the module-level variable.
                attribute_owners[f"{rel_file}:{var_name}"] = resolved

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

    # ── Phase 1A: Inheritance override edges ─────────────────────────────
    # When ChildClass overrides ParentClass.method, add child → parent edge.
    # Only child → parent direction: "if I changed the child, show me the
    # parent contract I might be violating." The reverse (parent → all 400
    # children) would create mega-hubs that destroy ranking.
    for child_key, bases in inheritance.items():
        child_file, child_class = child_key.split(":", 1)
        for qualifier, base_name in bases:
            base_owner = _resolve_owner_type(
                qualifier, base_name, child_file,
                import_maps.get(child_file, {}),
                class_registry, factory_returns, import_maps, repo_path,
                classes_by_file=classes_by_file,
            )
            if not base_owner:
                continue

            # Find all methods in the child class and look for same-named parent methods
            child_prefix = f"{child_key}."
            for fid in function_ids:
                if not fid.startswith(child_prefix):
                    continue
                method_name = fid[len(child_prefix):]
                parent_method = f"{base_owner}.{method_name}"
                if parent_method in function_ids and parent_method != fid:
                    # Child → parent only (avoids mega-hub on parent side)
                    if parent_method not in graph.get(fid, []):
                        graph.setdefault(fid, []).append(parent_method)

    # ── Phase 1B: Decorator edges ─────────────────────────────────────────
    # A function decorated with @my_decorator has an implicit dependency on
    # my_decorator. This is one of the strongest co-change signals:
    # if the decorator changes, all its callsites likely change too.
    for relative_file, tree in file_trees.items():
        import_map = import_maps[relative_file]
        for fn_node, _is_method, _class_name in _collect_function_nodes(tree):
            added = 0
            for deco in fn_node.decorator_list:
                if added >= _DECORATOR_EDGE_MAX:
                    break
                # @plain_name  or  @module.name  or  @name(args)
                deco_func = deco.func if isinstance(deco, ast.Call) else deco
                dep = None
                if isinstance(deco_func, ast.Name):
                    dep = _lookup(
                        deco_func.id,
                        {fid.split(":", 1)[1]: fid
                         for fid in functions
                         if fid.startswith(relative_file + ":")},
                        import_map, functions, repo_path,
                    )
                elif isinstance(deco_func, ast.Attribute) and isinstance(deco_func.value, ast.Name):
                    if deco_func.value.id in import_map:
                        src = import_map[deco_func.value.id]
                        rel_src = "./" + os.path.relpath(src, repo_path)
                        dep = f"{rel_src}:{deco_func.attr}"
                        if dep not in function_ids:
                            dep = None
                if dep:
                    fn_name_local = (
                        f"{_class_name}.{fn_node.name}"
                        if _class_name else fn_node.name
                    )
                    fid = f"{relative_file}:{fn_name_local}"
                    if fid in function_ids and dep != fid and dep not in graph.get(fid, []):
                        graph.setdefault(fid, []).append(dep)
                        added += 1

    # ── Build file_groups for use by shared-import edges ────────────────────
    file_groups: Dict[str, List[str]] = {}
    for fid in function_ids:
        ffile = fid.split(":")[0]
        file_groups.setdefault(ffile, []).append(fid)

    # ── Phase 1C: Shared-import consumer edges ───────────────────────────
    # If file_a and file_b both import from the same internal module,
    # functions in file_a and file_b are likely to co-change when that
    # module changes. Create edges between functions across those files.
    import_consumers: Dict[str, List[str]] = {}
    for rel_file, imap in import_maps.items():
        for _local_name, abs_path in imap.items():
            # Only track internal (in-repo) imports
            rel_imported = "./" + os.path.relpath(abs_path, repo_path)
            if not rel_imported.startswith("./"):
                continue
            import_consumers.setdefault(rel_imported, []).append(rel_file)

    # Deduplicate consumer file lists, then connect representatives
    for _imported_mod, consumer_files in import_consumers.items():
        consumer_files = list(dict.fromkeys(consumer_files))  # deduplicate, preserve order
        if len(consumer_files) < 2 or len(consumer_files) > _SHARED_IMPORT_MAX_CONSUMERS:
            continue
        # Pick a representative symbol from each consumer file
        representatives = []
        for cfile in consumer_files:
            rep_syms = file_groups.get(cfile, [])
            if rep_syms:
                representatives.append(rep_syms[0])
        # Connect representatives pairwise
        for i, a in enumerate(representatives):
            for b in representatives[i + 1:]:
                if b not in graph.get(a, []):
                    graph.setdefault(a, []).append(b)
                if a not in graph.get(b, []):
                    graph.setdefault(b, []).append(a)

    # ── Phase 1D: Same-directory sibling edges ────────────────────────────
    # Files in the same package directory tend to co-change (tests ↔ impl,
    # models ↔ serializers, etc.).  Connect one representative per file to
    # one representative from every other file in the same directory.
    # Cap: skip directories with >_SAME_DIR_MAX_FILES Python files to avoid
    # linking unrelated utility grab-bags.
    dir_files: Dict[str, List[str]] = {}
    for rel_file in file_groups:
        dir_part = os.path.dirname(rel_file)
        dir_files.setdefault(dir_part, []).append(rel_file)

    for _dir, dir_file_list in dir_files.items():
        if len(dir_file_list) < 2 or len(dir_file_list) > _SAME_DIR_MAX_FILES:
            continue
        reps = []
        for df in dir_file_list:
            syms = file_groups.get(df, [])
            if syms:
                reps.append(syms[0])   # one rep per file
        for i, a in enumerate(reps):
            for b in reps[i + 1:]:
                if b not in graph.get(a, []):
                    graph.setdefault(a, []).append(b)
                if a not in graph.get(b, []):
                    graph.setdefault(b, []).append(a)

    # ── Phase 1E: Sliding-window within-file edges ────────────────────────
    # Functions defined near each other in the same file are empirically the
    # strongest co-change signal after direct calls.  A sliding window of
    # WINDOW_SIZE links each function to its closest neighbours in definition
    # order.  Cap: skip files with >FILE_WINDOW_MAX_SYMS symbols (e.g. a
    # 600-function god-file would create O(n*w) noise edges).
    WINDOW_SIZE = 3          # each function linked to ±3 neighbours
    FILE_WINDOW_MAX_SYMS = 60  # skip window edges in very large files

    for rel_file, syms_in_file in file_groups.items():
        if len(syms_in_file) < 2 or len(syms_in_file) > FILE_WINDOW_MAX_SYMS:
            continue
        for i, a in enumerate(syms_in_file):
            for j in range(i + 1, min(i + 1 + WINDOW_SIZE, len(syms_in_file))):
                b = syms_in_file[j]
                if b not in graph.get(a, []):
                    graph.setdefault(a, []).append(b)
                if a not in graph.get(b, []):
                    graph.setdefault(b, []).append(a)

    # ── Phase 1F: Light parent→child inheritance edges ────────────────────
    # Child→parent already exists (Phase 1A).  For parents with FEW children
    # (≤PARENT_CHILD_MAX_CHILDREN), also add parent→child so that a change
    # to the parent method surfaces its direct overriders.  We skip parents
    # with many children to avoid creating mega-hubs (e.g. BaseModel in
    # pydantic has 400+ subclasses — those would destroy ranking).
    PARENT_CHILD_MAX_CHILDREN = 8

    # Build a map: parent_method_id -> [child_method_ids]
    parent_to_children: Dict[str, List[str]] = {}
    for child_key, bases in inheritance.items():
        child_file, child_class = child_key.split(":", 1)
        for qualifier, base_name in bases:
            base_owner = _resolve_owner_type(
                qualifier, base_name, child_file,
                import_maps.get(child_file, {}),
                class_registry, factory_returns, import_maps, repo_path,
                classes_by_file=classes_by_file,
            )
            if not base_owner:
                continue
            child_prefix = f"{child_key}."
            for fid in function_ids:
                if not fid.startswith(child_prefix):
                    continue
                method_name = fid[len(child_prefix):]
                parent_method = f"{base_owner}.{method_name}"
                if parent_method in function_ids and parent_method != fid:
                    parent_to_children.setdefault(parent_method, []).append(fid)

    for parent_method, children in parent_to_children.items():
        if len(children) > PARENT_CHILD_MAX_CHILDREN:
            continue   # too many — would create a mega-hub
        for child_fid in children:
            if child_fid not in graph.get(parent_method, []):
                graph.setdefault(parent_method, []).append(child_fid)

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


def _find_annotated_return(node):
    """
    Extract (qualifier, bare_name) from a PEP-3107 return annotation.

        def f() -> MyClass: ...         ->  (None, "MyClass")
        def f() -> module.MyClass: ...  ->  ("module", "MyClass")

    Skips primitive annotations (str, int, bool, None, etc.) and generics
    like List[X] where the outer type is not a class we own.
    """
    PRIMITIVES = frozenset({
        "str", "int", "float", "bool", "bytes", "None",
        "list", "dict", "set", "tuple", "Any", "Optional",
        "List", "Dict", "Set", "Tuple", "Iterator", "Generator",
        "Iterable", "Sequence", "Mapping", "Type", "Union",
    })
    ann = node.returns
    if ann is None:
        return None
    if isinstance(ann, ast.Constant):
        return None
    if isinstance(ann, ast.Name):
        if ann.id in PRIMITIVES:
            return None
        return (None, ann.id)
    if isinstance(ann, ast.Attribute) and isinstance(ann.value, ast.Name):
        if ann.attr in PRIMITIVES:
            return None
        return (ann.value.id, ann.attr)
    # Subscript like Optional[Router] — unwrap one level
    if isinstance(ann, ast.Subscript):
        return _find_annotated_return(
            type("_Stub", (), {"returns": ann.slice})()  # type: ignore[arg-type]
        )
    return None


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

    # Module-level variable fallback: `app = Flask()` at module scope.
    # attribute_owners stores these as "{rel_file}:{var_name}".
    if isinstance(node, ast.Name) and attribute_owners:
        module_key = f"{relative_file}:{node.id}"
        if module_key in attribute_owners:
            return attribute_owners[module_key]

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