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
from typing import Dict, List, Optional, Tuple

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


def build_repository_graph(
    repo_path: str,
    functions: Optional[Dict[str, object]] = None,
    file_trees: Optional[Dict[str, ast.Module]] = None,
    import_maps: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, List[str]]:
    """
    Build the complete call graph for a repository.

    Args:
        repo_path:   Repository root.
        functions:   Pre-extracted symbol table (id -> Symbol). Extracted
                     fresh when None.
        file_trees:  Pre-parsed ASTs keyed by relative file ("./x.py").
                     When provided, no file is read or parsed here — this is
                     how the pipeline avoids double-parsing every file.
        import_maps: Pre-built import maps keyed by relative file. Built
                     from `file_trees` when None.

    Returns:
        dict mapping function_id -> [list of called function_ids]
    """
    repo_path = os.path.abspath(repo_path)

    if functions is None:
        functions = extract_all_symbols(repo_path)
    function_ids = set(functions)

    ids_by_file, methods_by_class = _group_symbol_ids(functions)

    # ── pre-pass: per-file ASTs, import maps, class registry ─────────────
    if file_trees is None:
        file_trees = _parse_repo_files(repo_path)
    if import_maps is None:
        import_maps = {}
    _ensure_import_maps(file_trees, import_maps, repo_path)

    (class_registry, classes_by_file, inheritance,
     factory_returns, module_var_types) = _scan_module_level(file_trees)

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

    attribute_owners = _build_attribute_owners(
        file_trees, import_maps, module_var_types, repo_path,
        class_registry, classes_by_file, factory_returns,
        _cached_resolve_owner_type,
    )

    graph: Dict[str, List[str]] = {}
    _build_call_edges(
        graph, file_trees, import_maps, ids_by_file, functions, function_ids,
        repo_path, attribute_owners, inheritance, class_registry,
        factory_returns, classes_by_file, _cached_resolve_owner_type,
    )

    # Resolved once, used by both inheritance phases (1A and 1F below);
    # resolution inputs don't change between them.
    override_pairs = list(_iter_override_pairs(
        inheritance, methods_by_class, function_ids, import_maps,
        class_registry, factory_returns, repo_path, classes_by_file,
    ))

    _add_override_edges(graph, override_pairs)
    _add_decorator_edges(
        graph, file_trees, import_maps, ids_by_file,
        functions, function_ids, repo_path,
    )
    file_groups = _build_file_groups(function_ids, functions)
    _add_shared_import_edges(graph, import_maps, file_groups, repo_path)
    _add_same_directory_edges(graph, file_groups)
    _add_window_edges(graph, file_groups)
    _add_parent_child_edges(graph, override_pairs)

    return graph


# ── Build phases (extracted from build_repository_graph) ──────────────────

def _group_symbol_ids(functions):
    """
    Group symbol ids once, by file and by class.

    Returns (ids_by_file, methods_by_class):
      ids_by_file:      rel_file -> {name: fid}
      methods_by_class: "file:Class" -> [(fid, method_name)]

    The per-file and per-class lookups used to rescan every id with
    startswith — per file, per decorator, and per inheritance pair
    (measured: 48M startswith calls, ~11s of a 41s cold build on django).
    """
    ids_by_file:      Dict[str, Dict[str, str]]        = {}
    methods_by_class: Dict[str, List[Tuple[str, str]]] = {}
    for fid in functions:
        fid_file, fid_name = fid.split(":", 1)
        ids_by_file.setdefault(fid_file, {})[fid_name] = fid
        if "." in fid_name:
            cls_part, meth_part = fid_name.split(".", 1)
            methods_by_class.setdefault(f"{fid_file}:{cls_part}", []).append((fid, meth_part))
    return ids_by_file, methods_by_class


def _parse_repo_files(repo_path):
    """Read and parse every Python file: rel_file ("./x.py") -> ast.Module."""
    file_trees: Dict[str, ast.Module] = {}
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

        file_trees["./" + os.path.relpath(filename, repo_path)] = tree
    return file_trees


def _ensure_import_maps(file_trees, import_maps, repo_path):
    """Build the import map for any file that doesn't have one yet."""
    for relative_file, tree in file_trees.items():
        if relative_file not in import_maps:
            abs_file = os.path.join(repo_path, relative_file[2:])
            import_maps[relative_file] = build_import_map(
                abs_file, repo_path, tree=tree
            )


def _call_type_ref(func):
    """(qualifier, bare_name) for the callee of `x = SomeClass(...)`."""
    if isinstance(func, ast.Name):
        return (None, func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr)
    return None


def _class_bases(node):
    """(qualifier, bare_name) refs for a ClassDef's base classes."""
    bases = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            bases.append((None, b.id))
        elif isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name):
            bases.append((b.value.id, b.attr))
        elif isinstance(b, ast.Attribute):
            bases.append((None, b.attr))
    return bases


def _register_factory_return(node, relative_file, factory_returns):
    """Record the type a module-level function returns, if resolvable from
    its return statements or (as fallback) its return annotation."""
    ref = _find_return_type(node)
    if ref:
        factory_returns[f"{relative_file}:{node.name}"] = ref
    ann_ref = _find_annotated_return(node)
    if ann_ref and not ref:
        factory_returns[f"{relative_file}:{node.name}"] = ann_ref


def _module_var_bindings(node):
    """
    Yield (var_name, type_ref) for module-level constructor assignments:

        app = Flask(__name__)           (Assign)
        db: SQLAlchemy = SQLAlchemy(app)  (AnnAssign)
    """
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
        type_ref = _call_type_ref(node.value.func)
        if type_ref:
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    yield tgt.id, type_ref
    elif isinstance(node, ast.AnnAssign) and node.value and isinstance(node.value, ast.Call):
        type_ref = _call_type_ref(node.value.func)
        if type_ref and isinstance(node.target, ast.Name):
            yield node.target.id, type_ref


def _scan_module_level(file_trees):
    """
    One pass over every module's top-level statements. Returns:

      class_registry:   class_name -> [rel_file, ...]
      classes_by_file:  rel_file -> [class_name, ...]
      inheritance:      "rel_file:ClassName" -> bases
      factory_returns:  "rel_file:func" -> (qualifier, type)
      module_var_types: rel_file -> {var_name: (qualifier, bare_name)}
    """
    class_registry:  Dict[str, List[str]]         = {}
    classes_by_file: Dict[str, List[str]]         = {}
    inheritance:     Dict[str, List[Tuple]]       = {}
    factory_returns: Dict[str, Tuple]             = {}
    module_var_types: Dict[str, Dict[str, Tuple]] = {}

    for relative_file, tree in file_trees.items():
        mvt: Dict[str, Tuple] = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_registry.setdefault(node.name, []).append(relative_file)
                classes_by_file.setdefault(relative_file, []).append(node.name)
                inheritance[f"{relative_file}:{node.name}"] = _class_bases(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _register_factory_return(node, relative_file, factory_returns)
            else:
                for var_name, type_ref in _module_var_bindings(node):
                    mvt[var_name] = type_ref
        if mvt:
            module_var_types[relative_file] = mvt

    return (class_registry, classes_by_file, inheritance,
            factory_returns, module_var_types)


def _build_attribute_owners(
    file_trees, import_maps, module_var_types, repo_path,
    class_registry, classes_by_file, factory_returns, cached_resolve,
):
    """
    Map "rel_file:Class.attr" -> owning class id for every attribute whose
    type could be resolved. Module-level vars are registered too (keyed
    "rel_file:var_name") so that `app.run()` in a function body resolves
    to Flask.run.
    """
    attribute_owners: Dict[str, str] = {}

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
                attribute_owners[f"{rel_file}:{var_name}"] = resolved

    for relative_file, tree in file_trees.items():
        import_map = import_maps[relative_file]
        raw_own = extract_attribute_ownerships(tree)

        for key, type_ref in raw_own.items():
            if type_ref is None:
                continue
            qualifier, bare_name = type_ref
            resolved = cached_resolve(qualifier, bare_name, relative_file, import_map)
            if resolved:
                attribute_owners[f"{relative_file}:{key}"] = resolved

    return attribute_owners


def _build_call_edges(
    graph, file_trees, import_maps, ids_by_file, functions, function_ids,
    repo_path, attribute_owners, inheritance, class_registry,
    factory_returns, classes_by_file, cached_resolve,
):
    """Direct call edges plus function-reference-as-argument edges."""
    for relative_file, tree in file_trees.items():
        import_map = import_maps[relative_file]
        local_name_to_id = ids_by_file.get(relative_file, {})
        for fn_node, is_method, class_name in _collect_function_nodes(tree):
            _add_function_call_edges(
                graph, fn_node, is_method, class_name, relative_file,
                local_name_to_id, import_map, functions, function_ids,
                repo_path, attribute_owners, inheritance, class_registry,
                factory_returns, import_maps, classes_by_file, cached_resolve,
            )


def _add_function_call_edges(
    graph, fn_node, is_method, class_name, relative_file,
    local_name_to_id, import_map, functions, function_ids,
    repo_path, attribute_owners, inheritance, class_registry,
    factory_returns, import_maps, classes_by_file, cached_resolve,
):
    """Edges out of one function: every call it makes, plus function
    references it passes as arguments."""
    function_name = f"{class_name}.{fn_node.name}" if class_name else fn_node.name
    function_id   = f"{relative_file}:{function_name}"

    graph.setdefault(function_id, [])

    param_types    = extract_param_types(fn_node)
    local_var_types = {**param_types, **extract_local_var_types(fn_node, param_types)}
    param_names    = _all_param_names(fn_node)

    for child in ast.walk(fn_node):
        if not isinstance(child, ast.Call):
            continue

        dep = _resolve_call(
            child, is_method, class_name, relative_file,
            local_name_to_id, import_map, functions, function_ids,
            repo_path, attribute_owners, inheritance, class_registry,
            factory_returns, import_maps, local_var_types,
            classes_by_file, cached_resolve,
        )
        if dep and dep != function_id and dep not in graph[function_id]:
            graph[function_id].append(dep)

        _add_arg_reference_edges(
            graph, child, function_id, param_names, local_var_types,
            is_method, class_name, relative_file, local_name_to_id,
            import_map, functions, function_ids, repo_path,
            attribute_owners, inheritance, class_registry,
            factory_returns, import_maps, classes_by_file, cached_resolve,
        )


def _add_arg_reference_edges(
    graph, call_node, function_id, param_names, local_var_types,
    is_method, class_name, relative_file, local_name_to_id,
    import_map, functions, function_ids, repo_path,
    attribute_owners, inheritance, class_registry,
    factory_returns, import_maps, classes_by_file, cached_resolve,
):
    """Function references passed as arguments are dependencies too:
    `partial(black.format_file_contents, ...)` in blackd never calls the
    function, but a change to it absolutely lands in blackd's blast
    radius. Covers positional and keyword args (`sorted(xs, key=fn)`)."""
    for arg in list(call_node.args) + [kw.value for kw in call_node.keywords]:
        if isinstance(arg, ast.Name):
            # Parameters/locals shadow module-level functions —
            # `run(task)` where task is a param is not a
            # reference to a same-named function.
            if arg.id in param_names or arg.id in local_var_types:
                continue
        elif not isinstance(arg, ast.Attribute):
            continue

        ref = _resolve_func_expr(
            arg, is_method, class_name, relative_file,
            local_name_to_id, import_map, functions, function_ids,
            repo_path, attribute_owners, inheritance, class_registry,
            factory_returns, import_maps, local_var_types,
            classes_by_file, cached_resolve,
        )
        if ref and ref != function_id and ref not in graph[function_id]:
            graph[function_id].append(ref)


def _iter_override_pairs(
    inheritance, methods_by_class, function_ids, import_maps,
    class_registry, factory_returns, repo_path, classes_by_file,
):
    """Yield (child_method_id, parent_method_id) for every method that
    overrides a same-named method on a resolvable base class."""
    for child_key, bases in inheritance.items():
        child_file, _child_class = child_key.split(":", 1)
        for qualifier, base_name in bases:
            base_owner = _resolve_owner_type(
                qualifier, base_name, child_file,
                import_maps.get(child_file, {}),
                class_registry, factory_returns, import_maps, repo_path,
                classes_by_file=classes_by_file,
            )
            if not base_owner:
                continue
            for fid, method_name in methods_by_class.get(child_key, []):
                parent_method = f"{base_owner}.{method_name}"
                if parent_method in function_ids and parent_method != fid:
                    yield fid, parent_method


def _add_override_edges(graph, override_pairs):
    """Phase 1A: inheritance override edges. When ChildClass overrides
    ParentClass.method, add the child → parent edge. Only child → parent
    direction: "if I changed the child, show me the parent contract I
    might be violating." The reverse (parent → all 400 children) would
    create mega-hubs that destroy ranking."""
    for fid, parent_method in override_pairs:
        if parent_method not in graph.get(fid, []):
            graph.setdefault(fid, []).append(parent_method)


def _resolve_decorator_ref(
    deco, relative_file, ids_by_file, import_map,
    functions, function_ids, repo_path,
):
    """Function id a decorator expression refers to, or None.
    Handles @plain_name, @module.name, and @name(args)."""
    deco_func = deco.func if isinstance(deco, ast.Call) else deco
    if isinstance(deco_func, ast.Name):
        return _lookup(
            deco_func.id,
            ids_by_file.get(relative_file, {}),
            import_map, functions, repo_path,
        )
    if isinstance(deco_func, ast.Attribute) and isinstance(deco_func.value, ast.Name):
        if deco_func.value.id in import_map:
            src = import_map[deco_func.value.id]
            rel_src = "./" + os.path.relpath(src, repo_path)
            dep = f"{rel_src}:{deco_func.attr}"
            if dep in function_ids:
                return dep
    return None


def _add_decorator_edges(
    graph, file_trees, import_maps, ids_by_file,
    functions, function_ids, repo_path,
):
    """Phase 1B: decorator edges. A function decorated with @my_decorator
    has an implicit dependency on my_decorator. This is one of the
    strongest co-change signals: if the decorator changes, all its
    callsites likely change too."""
    for relative_file, tree in file_trees.items():
        import_map = import_maps[relative_file]
        for fn_node, _is_method, _class_name in _collect_function_nodes(tree):
            added = 0
            for deco in fn_node.decorator_list:
                if added >= _DECORATOR_EDGE_MAX:
                    break
                dep = _resolve_decorator_ref(
                    deco, relative_file, ids_by_file, import_map,
                    functions, function_ids, repo_path,
                )
                if not dep:
                    continue
                fn_name_local = (
                    f"{_class_name}.{fn_node.name}"
                    if _class_name else fn_node.name
                )
                fid = f"{relative_file}:{fn_name_local}"
                if fid in function_ids and dep != fid and dep not in graph.get(fid, []):
                    graph.setdefault(fid, []).append(dep)
                    added += 1


def _build_file_groups(function_ids, functions):
    """
    rel_file -> [fids], sorted by line number within each file: window
    edges genuinely follow definition order, and representative picks
    (syms[0]) are deterministic. Iteration is over sorted(function_ids)
    so the dict *key* order — the order Phases 1C-1E walk files when
    emitting edges — is hash-seed independent too; sorting only the
    within-file lists still left edge order varying between runs.
    """
    file_groups: Dict[str, List[str]] = {}
    for fid in sorted(function_ids):
        ffile = fid.split(":")[0]
        file_groups.setdefault(ffile, []).append(fid)
    for ffile in file_groups:
        file_groups[ffile].sort(
            key=lambda fid: (getattr(functions.get(fid), "lineno", 0) or 0, fid)
        )
    return file_groups


def _connect_pair(graph, a, b):
    """Add an undirected co-change edge (both directions, deduplicated)."""
    if b not in graph.get(a, []):
        graph.setdefault(a, []).append(b)
    if a not in graph.get(b, []):
        graph.setdefault(b, []).append(a)


def _connect_file_representatives(graph, files, file_groups):
    """Connect one representative symbol per file, pairwise."""
    representatives = []
    for cfile in files:
        rep_syms = file_groups.get(cfile, [])
        if rep_syms:
            representatives.append(rep_syms[0])   # one rep per file
    for i, a in enumerate(representatives):
        for b in representatives[i + 1:]:
            _connect_pair(graph, a, b)


def _add_shared_import_edges(graph, import_maps, file_groups, repo_path):
    """Phase 1C: shared-import consumer edges. If file_a and file_b both
    import from the same internal module, functions in file_a and file_b
    are likely to co-change when that module changes. Create edges between
    representatives of those files, skipping modules with more than
    _SHARED_IMPORT_MAX_CONSUMERS consumers."""
    import_consumers: Dict[str, List[str]] = {}
    for rel_file, imap in import_maps.items():
        for _local_name, abs_path in imap.items():
            # Only track internal (in-repo) imports
            rel_imported = "./" + os.path.relpath(abs_path, repo_path)
            if not rel_imported.startswith("./"):
                continue
            import_consumers.setdefault(rel_imported, []).append(rel_file)

    for _imported_mod, consumer_files in import_consumers.items():
        consumer_files = list(dict.fromkeys(consumer_files))  # deduplicate, preserve order
        if len(consumer_files) < 2 or len(consumer_files) > _SHARED_IMPORT_MAX_CONSUMERS:
            continue
        _connect_file_representatives(graph, consumer_files, file_groups)


def _add_same_directory_edges(graph, file_groups):
    """Phase 1D: same-directory sibling edges. Files in the same package
    directory tend to co-change (tests ↔ impl, models ↔ serializers,
    etc.). Connect one representative per file to one representative from
    every other file in the same directory. Cap: skip directories with
    >_SAME_DIR_MAX_FILES Python files to avoid linking unrelated utility
    grab-bags."""
    dir_files: Dict[str, List[str]] = {}
    for rel_file in file_groups:
        dir_part = os.path.dirname(rel_file)
        dir_files.setdefault(dir_part, []).append(rel_file)

    for _dir, dir_file_list in dir_files.items():
        if len(dir_file_list) < 2 or len(dir_file_list) > _SAME_DIR_MAX_FILES:
            continue
        _connect_file_representatives(graph, dir_file_list, file_groups)


def _add_window_edges(graph, file_groups):
    """Phase 1E: sliding-window within-file edges. Functions defined near
    each other in the same file are empirically the strongest co-change
    signal after direct calls. A sliding window of WINDOW_SIZE links each
    function to its closest neighbours in definition order. Cap: skip
    files with >FILE_WINDOW_MAX_SYMS symbols (e.g. a 600-function
    god-file would create O(n*w) noise edges)."""
    WINDOW_SIZE = 3          # each function linked to ±3 neighbours
    FILE_WINDOW_MAX_SYMS = 60  # skip window edges in very large files

    for rel_file, syms_in_file in file_groups.items():
        if len(syms_in_file) < 2 or len(syms_in_file) > FILE_WINDOW_MAX_SYMS:
            continue
        for i, a in enumerate(syms_in_file):
            for j in range(i + 1, min(i + 1 + WINDOW_SIZE, len(syms_in_file))):
                _connect_pair(graph, a, syms_in_file[j])


def _add_parent_child_edges(graph, override_pairs):
    """Phase 1F: light parent→child inheritance edges. Child→parent
    already exists (Phase 1A). For parents with FEW children
    (≤PARENT_CHILD_MAX_CHILDREN), also add parent→child so that a change
    to the parent method surfaces its direct overriders. We skip parents
    with many children to avoid creating mega-hubs (e.g. BaseModel in
    pydantic has 400+ subclasses — those would destroy ranking)."""
    PARENT_CHILD_MAX_CHILDREN = 8

    parent_to_children: Dict[str, List[str]] = {}
    for fid, parent_method in override_pairs:
        parent_to_children.setdefault(parent_method, []).append(fid)

    for parent_method, children in parent_to_children.items():
        if len(children) > PARENT_CHILD_MAX_CHILDREN:
            continue   # too many — would create a mega-hub
        for child_fid in children:
            if child_fid not in graph.get(parent_method, []):
                graph.setdefault(parent_method, []).append(child_fid)


# ── Internal helpers ──────────────────────────────────────────────────────

def _collect_function_nodes(tree):
    """
    Return list of (function_node, is_method, class_name) for EVERY function
    in the file, including nested functions and closures.

    Mirrors what parser.py's _collect_functions does so that the graph covers
    every symbol the parser emits (previously missed ~30-40% of nodes) —
    with one known gap: this collector does not descend into if/try/with
    blocks, so a `def` under `if TYPE_CHECKING:` or `try/except ImportError`
    is parsed as a symbol but gets no graph edges.
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
    return _resolve_func_expr(
        call_node.func, is_method, class_name, relative_file,
        local_name_to_id, import_map, all_functions, function_ids,
        repo_path, attribute_owners, inheritance, class_registry,
        factory_returns, import_maps, local_var_types,
        classes_by_file, _cached_resolve,
    )


def _resolve_func_expr(
    func, is_method, class_name, relative_file,
    local_name_to_id, import_map, all_functions, function_ids,
    repo_path, attribute_owners, inheritance, class_registry,
    factory_returns, import_maps, local_var_types=None,
    classes_by_file=None, _cached_resolve=None,
):
    """Resolve a Name/Attribute expression that denotes a function — either
    the callee of a Call node or a function reference passed as an argument
    (`partial(black.format_file_contents, ...)`, `sorted(xs, key=fn)`)."""

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

        # Module-attribute call: `black.format_file_contents(...)` after
        # `import black`, or `mypkg.core.fn(...)` after `import mypkg.core`.
        dotted = _dotted_module_name(func.value)
        if dotted and dotted in import_map:
            source_file = import_map[dotted]
            relative_source = "./" + os.path.relpath(source_file, repo_path)
            candidate = f"{relative_source}:{method_name}"
            if candidate in all_functions:
                return candidate
            # __init__ transparency: the attribute may be re-exported from a
            # submodule (`black.parse_ast` lives in black/parsing.py). The
            # __init__.py's own import map IS its re-export table — no
            # re-parsing needed.
            if relative_source.endswith("__init__.py"):
                init_map = import_maps.get(relative_source, {})
                target = init_map.get(method_name)
                if target:
                    real_source = "./" + os.path.relpath(target, repo_path)
                    candidate = f"{real_source}:{method_name}"
                    if candidate in all_functions:
                        return candidate

    return None


def _all_param_names(fn_node):
    """Every parameter name of a function, including * / ** and kw-only."""
    a = fn_node.args
    names = {p.arg for p in a.args + a.posonlyargs + a.kwonlyargs}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _dotted_module_name(expr):
    """`Name(a)` → "a"; `Attribute(Name(a), b)` → "a.b"; anything else → None."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _dotted_module_name(expr.value)
        if base:
            return f"{base}.{expr.attr}"
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