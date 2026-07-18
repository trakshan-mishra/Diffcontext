"""
resolver.py — Resolve Python import statements to filesystem paths.
"""

import ast
import logging
import os
from collections import deque
from typing import Dict, List, Optional, Tuple

from ._warn_once import warn_syntax_error_once, check_and_warn_encoding

logger = logging.getLogger(__name__)


# Import statements can only appear in statement blocks — never inside an
# expression — so import scanning walks only these fields instead of every
# AST node (expressions dominate node count ~10:1). Field order mirrors the
# AST's own field order so import precedence matches a full BFS walk.
_STMT_BLOCK_FIELDS = ("body", "handlers", "orelse", "finalbody", "cases")


def _iter_import_nodes(tree: "ast.Module"):
    """Yield every Import/ImportFrom in the tree, in BFS document order,
    without descending into expression subtrees."""
    queue = deque(tree.body)
    while queue:
        node = queue.popleft()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
            continue
        for field in _STMT_BLOCK_FIELDS:
            block = getattr(node, field, None)
            if block:
                queue.extend(block)


def build_import_map(
    filename: str,
    repo_path: str,
    tree: "Optional[ast.Module]" = None,
) -> Dict[str, str]:
    """
    Parse imports in a file and resolve them to absolute paths.

    Returns:
        dict: local_name -> absolute_path_of_source_file
        e.g. {"helper": "/repo/utils.py", "Session": "/repo/sessions.py"}

    __init__.py transparency:
        When an import resolves to a package __init__.py, we scan that
        __init__.py for re-export statements (`from .sub import Name`) and
        follow them to the actual definition file. This means:

            from flask import Flask
            # resolves to src/flask/__init__.py
            # __init__.py has: from .app import Flask
            # final result: src/flask/app.py   ← correct
    """
    # Accept a pre-parsed AST to avoid re-reading and re-parsing the file
    # (the pipeline parses each file exactly once and shares the tree).
    if tree is None:
        with open(filename, "rb") as f:
            raw = f.read()
        check_and_warn_encoding(logger, filename, raw)
        source = raw.decode("utf-8", errors="ignore")

        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            warn_syntax_error_once(logger, filename, e)
            return {}

    imports: Dict[str, str] = {}
    file_dir = os.path.dirname(os.path.abspath(filename))
    repo_abs = os.path.abspath(repo_path)

    for node in _iter_import_nodes(tree):

        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = node.level

            if level > 0:
                # Relative import: from .utils import helper
                base = file_dir
                for _ in range(level - 1):
                    base = os.path.dirname(base)
                module_path = os.path.join(base, module.replace(".", os.sep))
                resolved = _resolve_module_path(module_path)
            else:
                # Absolute import: from requests.utils import helper
                # Resolved against every source root (repo root, then src/):
                # black, flask, and most modern PyPI projects keep their
                # packages under src/, where a repo-root-only lookup finds
                # nothing and silently drops every edge into the package.
                module_path, resolved = _resolve_absolute_module(
                    module, repo_abs
                )

            for alias in node.names:
                local_name = alias.asname or alias.name

                # Check if this is a submodule import (from package import module)
                submodule_path = os.path.join(module_path, alias.name)
                submodule_resolved = _resolve_module_path(submodule_path)

                if submodule_resolved:
                    imports[local_name] = submodule_resolved
                elif resolved:
                    # __init__.py transparency: follow re-exports one level
                    if resolved.endswith("__init__.py"):
                        real = _follow_init_reexport(resolved, alias.name, repo_abs)
                        imports[local_name] = real if real else resolved
                    else:
                        imports[local_name] = resolved

        elif isinstance(node, ast.Import):
            for alias in node.names:
                top_module = alias.name.split(".")[0]

                if alias.asname:
                    # `import a.b as ab` — ab refers to the submodule a.b
                    _mp, resolved = _resolve_absolute_module(alias.name, repo_abs)
                    if not resolved:
                        sibling = os.path.join(
                            file_dir, alias.name.replace(".", os.sep)
                        )
                        resolved = _resolve_module_path(sibling)
                    if resolved:
                        imports[alias.asname] = resolved
                    continue

                # `import a` / `import a.b` — the bound local name is the TOP
                # package `a` (binding it to a/b.py, as previously done, sent
                # `a.other()` calls into the wrong file).
                _mp, resolved = _resolve_absolute_module(top_module, repo_abs)
                if not resolved:
                    # Bare `import x` — try the importing file's own directory
                    sibling_path = os.path.join(
                        file_dir, top_module.replace(".", os.sep)
                    )
                    resolved = _resolve_module_path(sibling_path)
                if resolved:
                    imports[top_module] = resolved

                # `import a.b` also makes `a.b.fn()` callable — record the
                # full dotted path (dots can't collide with identifiers).
                if "." in alias.name:
                    _mp, sub_resolved = _resolve_absolute_module(
                        alias.name, repo_abs
                    )
                    if sub_resolved:
                        imports[alias.name] = sub_resolved

    return imports


# Conventional source roots tried, in order, when resolving absolute
# imports. "" is the repo root itself (flat layout); "src" is the
# setuptools src-layout used by black, flask, requests, and most modern
# PyPI projects.
_SOURCE_ROOT_NAMES = ("", "src")


def _resolve_absolute_module(module: str, repo_abs: str):
    """
    Resolve a dotted absolute module name against each candidate source
    root of the repository.

    Returns (module_path, resolved_file):
      module_path   — directory-ish path for the module under the root that
                      matched (used for submodule probing by the caller);
                      falls back to the repo-root join when nothing matched.
      resolved_file — the module's .py / package __init__.py, or None.
    """
    rel = module.replace(".", os.sep)
    fallback = os.path.join(repo_abs, rel)
    for root_name in _SOURCE_ROOT_NAMES:
        root = os.path.join(repo_abs, root_name) if root_name else repo_abs
        candidate = os.path.join(root, rel)
        resolved = _resolve_module_path(candidate)
        if resolved:
            return candidate, resolved
        # Namespace package (no __init__.py): a real directory still lets
        # the caller find `from pkg import submodule` targets inside it.
        if os.path.isdir(candidate):
            return candidate, None
    return fallback, None


# Session cache of parsed __init__.py re-export specs, keyed by absolute
# path and validated by (mtime_ns, size) so an edited file is re-read. A
# flagship package's __init__.py (django.db.models, flask) is consulted by
# hundreds of importing files per cold index; without this each consult
# re-read and re-parsed it from disk.
_init_export_cache: "Dict[str, Tuple[Tuple[int, int], Optional[Dict[str, List[Tuple[str, int, str]]]]]]" = {}


def _init_export_specs(
    init_path: str,
) -> "Optional[Dict[str, List[Tuple[str, int, str]]]]":
    """
    exported_name -> [(module, level, original_name), ...] for every
    `from X import Y` in the file, in document order. None when the file
    is unreadable or unparsable.
    """
    try:
        st = os.stat(init_path)
        stat_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    cached = _init_export_cache.get(init_path)
    if cached is not None and cached[0] == stat_key:
        return cached[1]

    specs: "Optional[Dict[str, List[Tuple[str, int, str]]]]"
    try:
        with open(init_path, "rb") as f:
            raw = f.read()
        tree = ast.parse(raw.decode("utf-8", errors="ignore"))
    except (OSError, SyntaxError):
        specs = None
    else:
        specs = {}
        for node in _iter_import_nodes(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                exported = alias.asname or alias.name
                specs.setdefault(exported, []).append(
                    (node.module or "", node.level, alias.name)
                )
    _init_export_cache[init_path] = (stat_key, specs)
    return specs


def _follow_init_reexport(init_path: str, name: str, repo_abs: str) -> Optional[str]:
    """
    Given a package __init__.py and a name imported from it, check whether
    __init__.py re-exports that name from a submodule.

    Example:
        __init__.py contains:  from .app import Flask
        name = "Flask"
        → returns /abs/path/to/app.py

    Both relative (`from .app import Flask`, flask style) and absolute
    (`from black.parsing import parse_ast`, black style) re-exports are
    followed. Only follows one level (no recursive re-export chasing) to
    stay fast. Returns None if the name is not re-exported or the
    submodule can't be found.
    """
    specs = _init_export_specs(init_path)
    if not specs:
        return None

    init_dir = os.path.dirname(init_path)

    for module, level, original_name in specs.get(name, ()):
        if level > 0:
            # Relative re-export: from .sub import X
            base = init_dir
            for _ in range(level - 1):
                base = os.path.dirname(base)
            sub_path = os.path.join(base, module.replace(".", os.sep))
        else:
            # Absolute re-export: from black.parsing import X
            sub_path, _resolved = _resolve_absolute_module(module, repo_abs)
        # Check if the original name itself is a submodule
        submodule_path = os.path.join(sub_path, original_name)
        resolved = _resolve_module_path(submodule_path) or _resolve_module_path(sub_path)
        if resolved and resolved != os.path.normpath(init_path):
            return resolved

    return None


def _resolve_module_path(module_path: str) -> Optional[str]:
    """Try to find a .py file or package __init__.py for a given path."""
    candidates = [
        module_path + ".py",
        os.path.join(module_path, "__init__.py"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.normpath(candidate)
    return None