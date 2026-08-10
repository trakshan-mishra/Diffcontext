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


def collect_functions(tree: "ast.Module") -> "List[tuple]":
    """
    Collect (qualified_name, node) for every function/method definition,
    including nested functions, methods of classes defined inside
    functions, and definitions under conditional blocks (`if
    TYPE_CHECKING:`, `try/except ImportError`, `match`).

    Names follow PEP 3155 (`__qualname__`), which is what the language
    itself calls these:

        Resource._add_nested_resources
        Resource._add_nested_resources.<locals>.createResourceMethod

    The `<locals>` segment is not decoration — without it a function
    nested inside a method is named for its CLASS only, so every nested
    `decorator` in a class collapses onto one id and all but the last is
    silently dropped from the index (measured: flask's
    `Blueprint.decorator` claimed by 4 distinct definitions, click's
    `Group.decorator` by 3; 48 definitions lost across 10 repos).

    This is the ONE place symbol names are constructed. graph_builder and
    the co-change miner both call it; when they each had their own copy
    they disagreed about nested functions, and the miner then produced
    ground-truth ids the index could not match (an automatic 0% recall
    that looked like a retrieval failure).

    Note the remaining known ambiguity, which qualnames do NOT resolve:
    `@property`/`@x.setter` pairs and `@typing.overload` stubs share a
    qualname with their sibling. Overload stubs collapsing onto the real
    implementation is desirable (last write wins, and the implementation
    is emitted last); property/setter pairs are a genuine gap.
    """
    collected: "List[tuple]" = []
    scope: "List[str]" = []

    def _walk(stmts):
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = ".".join(scope + [node.name]) if scope else node.name
                collected.append((name, node))
                # Anything defined inside a function body lives in its
                # <locals> namespace — PEP 3155's rule, and the reason two
                # same-named closures in one class stay distinct.
                scope.append(node.name + ".<locals>")
                _walk(node.body)
                scope.pop()
            elif isinstance(node, ast.ClassDef):
                scope.append(node.name)
                _walk(node.body)
                scope.pop()
            else:
                for field in _STMT_BLOCK_FIELDS:
                    block = getattr(node, field, None)
                    if block:
                        _walk(block)

    _walk(tree.body)
    return collected


# Back-compat alias: this was private before it acquired three callers.
_collect_functions = collect_functions


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