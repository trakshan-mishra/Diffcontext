"""
parser.py — AST-based symbol extraction from Python source files.

Extracts functions, methods (including async), with class-aware naming.
"""

import ast
import os
from typing import Dict

from .models import Symbol


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


def extract_symbols(filename: str, repo_path: str) -> Dict[str, Symbol]:
    """
    Parse a single Python file, return dict of symbol_id -> Symbol.

    Symbol IDs look like: "./relative/path.py:ClassName.method_name"
    """
    with open(filename, "r", encoding="utf-8", errors="ignore") as f:
        source = f.read()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    relative_file = "./" + os.path.relpath(filename, repo_path)
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


def extract_all_symbols(repo_path: str) -> Dict[str, Symbol]:
    """Extract symbols from all Python files in a repository."""
    from .scanner import find_python_files

    repo_path = os.path.abspath(repo_path)
    all_symbols: Dict[str, Symbol] = {}

    for filepath in find_python_files(repo_path):
        all_symbols.update(extract_symbols(filepath, repo_path))

    return all_symbols
