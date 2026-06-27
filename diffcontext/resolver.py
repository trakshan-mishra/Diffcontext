"""
resolver.py — Resolve Python import statements to filesystem paths.
"""

import ast
import logging
import os
from typing import Dict, Optional

from ._warn_once import warn_syntax_error_once, check_and_warn_encoding

logger = logging.getLogger(__name__)


def build_import_map(filename: str, repo_path: str) -> Dict[str, str]:
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

    for node in ast.walk(tree):

        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = node.level

            if level > 0:
                # Relative import: from .utils import helper
                base = file_dir
                for _ in range(level - 1):
                    base = os.path.dirname(base)
                module_path = os.path.join(base, module.replace(".", os.sep))
            else:
                # Absolute import: from requests.utils import helper
                module_path = os.path.join(repo_abs, module.replace(".", os.sep))

            resolved = _resolve_module_path(module_path)

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
                local_name = alias.asname or top_module
                module_path = os.path.join(repo_abs, alias.name.replace(".", os.sep))
                resolved = _resolve_module_path(module_path)

                if not resolved:
                    # Bare `import x` — try the importing file's own directory
                    sibling_path = os.path.join(file_dir, alias.name.replace(".", os.sep))
                    resolved = _resolve_module_path(sibling_path)

                if resolved:
                    imports[local_name] = resolved

    return imports


def _follow_init_reexport(init_path: str, name: str, repo_abs: str) -> Optional[str]:
    """
    Given a package __init__.py and a name imported from it, check whether
    __init__.py re-exports that name from a submodule.

    Example:
        __init__.py contains:  from .app import Flask
        name = "Flask"
        → returns /abs/path/to/app.py

    Only follows one level (no recursive re-export chasing) to stay fast.
    Returns None if the name is not re-exported or the submodule can't be found.
    """
    try:
        with open(init_path, "rb") as f:
            raw = f.read()
        source = raw.decode("utf-8", errors="ignore")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return None

    init_dir = os.path.dirname(init_path)

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            continue  # only relative re-exports (from .sub import X)
        for alias in node.names:
            exported_name = alias.asname or alias.name
            if exported_name != name:
                continue
            # Found the re-export — resolve the submodule
            module = node.module or ""
            base = init_dir
            for _ in range(node.level - 1):
                base = os.path.dirname(base)
            sub_path = os.path.join(base, module.replace(".", os.sep))
            # Check if alias.name itself is a submodule
            submodule_path = os.path.join(sub_path, alias.name)
            resolved = _resolve_module_path(submodule_path) or _resolve_module_path(sub_path)
            if resolved:
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