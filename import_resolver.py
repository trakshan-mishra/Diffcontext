import ast
import os


def build_import_map(filename, repo_path):
    """
    Parse imports in a file and resolve them to absolute paths.

    Returns:
        dict: local_name -> absolute_path_of_source_file
        e.g. {"helper": "/repo/utils.py", "Session": "/repo/sessions.py"}
    """
    with open(filename, "r", encoding="utf-8", errors="ignore") as f:
        source = f.read()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    imports = {}
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

                # `from . import routing` / `from fastapi import routing`:
                # `routing` is itself a submodule (routing.py), not a name
                # defined in the package's __init__.py. Prefer that
                # interpretation -- it's what matters for `routing.Foo`
                # style attribute access, which is the common case for
                # `from package import submodule` imports.
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
                if resolved:
                    imports[local_name] = resolved

    return imports


def _resolve_module_path(module_path):
    """Try to find a .py file or package __init__.py for a given path."""
    candidates = [
        module_path + ".py",
        os.path.join(module_path, "__init__.py"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.normpath(candidate)
    return None
