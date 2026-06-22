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
                    imports[local_name] = resolved

        elif isinstance(node, ast.Import):
            for alias in node.names:
                top_module = alias.name.split(".")[0]
                local_name = alias.asname or top_module
                module_path = os.path.join(repo_abs, alias.name.replace(".", os.sep))
                resolved = _resolve_module_path(module_path)

                if not resolved:
                    # Bare `import x` with no dots doesn't always live at the
                    # repo root -- a very common real pattern is sibling
                    # script files in the same directory doing `import store`,
                    # which only works at runtime because that directory is
                    # on sys.path (CWD, or an explicit sys.path.insert).
                    # Static analysis can't know the real sys.path, but
                    # "the importing file's own directory" covers the
                    # overwhelmingly common case correctly.
                    sibling_path = os.path.join(file_dir, alias.name.replace(".", os.sep))
                    resolved = _resolve_module_path(sibling_path)

                if resolved:
                    imports[local_name] = resolved

    return imports


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