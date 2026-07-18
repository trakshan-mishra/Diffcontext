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


# `def`/`class` are statements, so collection only needs to descend
# through statement blocks — expression subtrees (the majority of AST
# nodes) can never contain a definition. Field order mirrors the AST's
# own field order so collection order matches a full NodeVisitor walk.
_STMT_BLOCK_FIELDS = ("body", "handlers", "orelse", "finalbody", "cases")


def _collect_functions(tree: "ast.Module") -> "List[tuple]":
    """
    Collect (qualified_name, node) for every function/method definition,
    including nested functions, methods of classes defined inside
    functions, and definitions under conditional blocks (`if
    TYPE_CHECKING:`, `try/except ImportError`, `match`).
    """
    collected: "List[tuple]" = []
    class_stack: "List[str]" = []

    def _walk(stmts):
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if class_stack:
                    name = ".".join(class_stack) + "." + node.name
                else:
                    name = node.name
                collected.append((name, node))
                _walk(node.body)
            elif isinstance(node, ast.ClassDef):
                class_stack.append(node.name)
                _walk(node.body)
                class_stack.pop()
            else:
                for field in _STMT_BLOCK_FIELDS:
                    block = getattr(node, field, None)
                    if block:
                        _walk(block)

    _walk(tree.body)
    return collected


def _segment_lines(source: str) -> "Optional[List[str]]":
    """
    Pre-split source for fast per-symbol segment slicing.

    `ast.get_source_segment` re-splits the ENTIRE file for every symbol —
    on a large repo that is the single biggest cold-index cost. Splitting
    once per file and slicing per symbol is equivalent, but only when the
    file has no `\\r` or `\\f` characters (the parser's line accounting
    treats those specially); return None then, and the caller falls back
    to `ast.get_source_segment` for that file.
    """
    if "\r" in source or "\f" in source:
        return None
    return source.split("\n")


def _fast_segment(lines: "List[str]", node) -> "Optional[str]":
    """Slice a node's source from pre-split lines. AST column offsets are
    UTF-8 byte offsets, so non-ASCII boundary lines go through bytes."""
    end_lineno = getattr(node, "end_lineno", None)
    end_col = getattr(node, "end_col_offset", None)
    if end_lineno is None or end_col is None:
        return None
    lineno = node.lineno - 1
    end_lineno -= 1
    col = node.col_offset

    def _cols(line: str, start: "Optional[int]", end: "Optional[int]") -> str:
        if line.isascii():
            return line[start:end]
        return line.encode("utf-8")[start:end].decode("utf-8")

    if end_lineno == lineno:
        return _cols(lines[lineno], col, end_col)
    first = _cols(lines[lineno], col, None)
    last = _cols(lines[end_lineno], None, end_col)
    return "\n".join([first, *lines[lineno + 1 : end_lineno], last])


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

    seg_lines = _segment_lines(source)

    symbols = {}
    for name, node in _collect_functions(tree):
        symbol_id = f"{relative_file}:{name}"
        if seg_lines is not None:
            try:
                code = _fast_segment(seg_lines, node)
            except (IndexError, UnicodeError):
                code = ast.get_source_segment(source, node)
        else:
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