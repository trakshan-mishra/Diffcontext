"""
parser.py — AST-based symbol extraction from Python source files.

Extracts functions, methods (including async), with class-aware naming.
"""

import ast
import logging
import os
from typing import Dict, List, Optional

from .models import Symbol
from ._warn_once import warn_syntax_error_once, check_and_warn_encoding

logger = logging.getLogger(__name__)


class _FunctionCollector(ast.NodeVisitor):
    """AST visitor that collects all function/method definitions."""

    def __init__(self):
        self.class_stack: list = []
        self.collected: list = []

    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)
        for child in node.body:
            self.visit(child)
        self.class_stack.pop()

    def visit_FunctionDef(self, node):
        self._collect(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._collect(node)
        self.generic_visit(node)

    def _collect(self, node):
        if self.class_stack:
            name = ".".join(self.class_stack) + "." + node.name
        else:
            name = node.name
        self.collected.append((name, node))


def extract_symbols(
    filename: str,
    repo_path: str,
    broken_files: "Optional[List[str]]" = None,
    source: "Optional[str]" = None,
    tree: "Optional[ast.Module]" = None,
) -> Dict[str, Symbol]:
    """
    Parse a single Python file, return dict of symbol_id -> Symbol.

    Symbol IDs look like: "./relative/path.py:ClassName.method_name"

    If parsing fails and `broken_files` is provided (a list), the file's
    relative path is appended to it so callers can distinguish "file failed
    to parse" from "file legitimately has no functions."

    `source` and `tree` may be supplied together to reuse an already-read,
    already-parsed file (the pipeline parses each file exactly once and
    shares the result); both must correspond to the same file contents.
    """
    relative_file = "./" + os.path.relpath(filename, repo_path)

    if source is None or tree is None:
        with open(filename, "rb") as f:
            raw = f.read()
        check_and_warn_encoding(logger, filename, raw)
        source = raw.decode("utf-8", errors="ignore")

        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            warn_syntax_error_once(logger, filename, e)
            if broken_files is not None:
                broken_files.append(relative_file)
            return {}

    collector = _FunctionCollector()
    collector.visit(tree)

    symbols = {}
    for name, node in collector.collected:
        symbol_id = f"{relative_file}:{name}"
        code = ast.get_source_segment(source, node)
        if code is None:
            continue
        symbols[symbol_id] = Symbol(
            id=symbol_id,
            file=filename,
            name=name,
            code=code,
            lineno=node.lineno,
        )

    return symbols


def extract_all_symbols(
    repo_path: str,
    broken_files: "Optional[List[str]]" = None,
) -> Dict[str, Symbol]:
    """
    Extract symbols from all Python files in a repository.

    If `broken_files` is provided (a list), relative paths of any files
    that failed to parse (SyntaxError) are appended to it.
    """
    from .scanner import find_python_files
    from .cache import SymbolCache

    repo_path = os.path.abspath(repo_path)
    all_symbols: Dict[str, Symbol] = {}
    
    db_path = os.path.join(repo_path, ".diffcontext_cache.db")

    with SymbolCache(db_path) as cache:
        for filepath in find_python_files(repo_path):
            def _parse(path: str) -> Dict[str, Symbol]:
                return extract_symbols(path, repo_path, broken_files=broken_files)
            
            all_symbols.update(cache.get_or_parse(filepath, _parse))

    return all_symbols