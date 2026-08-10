#!/usr/bin/env python3
"""
tests/test_qualnames.py — PEP 3155 symbol naming, and the parser/graph
agreement it exists to guarantee.

Symbol ids are constructed in exactly one place (parser.collect_functions).
graph_builder consumes that walk rather than keeping its own copy. Both
halves of that arrangement are load-bearing and neither is obvious from
reading a diff, so they are pinned here:

  * `<locals>` keeps same-named closures distinct. Without it a function
    nested inside a method is named for its CLASS only, so every nested
    `decorator` in a class collapses onto one id and all but the last is
    dropped from the index.

  * graph_builder's old private walk did not descend into `if`/`try`/`match`
    blocks, so a `def` under `if TYPE_CHECKING:` was indexed as a symbol but
    received no graph node at all. Sharing one walk is what closes that gap,
    and only an integration assertion can catch it reopening.
"""

import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.parser import collect_functions, extract_all_symbols
from diffcontext.graph_builder import _enclosing_class, build_repository_graph


def _names(source):
    """Qualnames collect_functions emits for a source string."""
    return [name for name, _node in collect_functions(ast.parse(source))]


class TestQualnames:
    def test_toplevel_function_is_bare_name(self):
        assert _names("def f(): pass") == ["f"]

    def test_method_is_class_qualified(self):
        assert _names("class A:\n    def f(self): pass") == ["A.f"]

    def test_nested_class_chains(self):
        src = "class A:\n    class B:\n        def f(self): pass"
        assert _names(src) == ["A.B.f"]

    def test_nested_function_gets_locals_segment(self):
        src = "def f():\n    def g(): pass"
        assert _names(src) == ["f", "f.<locals>.g"]

    def test_nested_function_in_method(self):
        src = "class A:\n    def f(self):\n        def g(): pass"
        assert _names(src) == ["A.f", "A.f.<locals>.g"]

    def test_class_defined_inside_function(self):
        src = "def f():\n    class B:\n        def g(self): pass"
        assert _names(src) == ["f", "f.<locals>.B.g"]

    def test_async_functions_collected(self):
        src = "class A:\n    async def f(self):\n        async def g(): pass"
        assert _names(src) == ["A.f", "A.f.<locals>.g"]

    def test_same_named_closures_in_one_class_stay_distinct(self):
        """
        The flask `Blueprint.decorator` / click `Group.decorator` case: two
        methods of one class each nest a function with the SAME name. Under
        class-stack-only naming both became "A.decorator" and the first was
        silently dropped. This is the regression the <locals> segment exists
        to prevent, so assert distinctness explicitly and not just by shape.
        """
        src = (
            "class A:\n"
            "    def one(self):\n"
            "        def decorator(): pass\n"
            "    def two(self):\n"
            "        def decorator(): pass\n"
        )
        names = _names(src)
        assert "A.one.<locals>.decorator" in names
        assert "A.two.<locals>.decorator" in names
        assert len(names) == len(set(names)), f"duplicate ids: {names}"

    @pytest.mark.parametrize(
        "header",
        [
            "if TYPE_CHECKING:",
            "while True:",
            "for _ in range(1):",
            "with open('x') as fh:",
        ],
    )
    def test_definitions_under_statement_blocks_are_collected(self, header):
        assert _names(f"{header}\n    def f(): pass") == ["f"]

    def test_definition_in_except_handler_is_collected(self):
        src = "try:\n    import x\nexcept ImportError:\n    def f(): pass"
        assert _names(src) == ["f"]

    def test_definition_in_finally_is_collected(self):
        src = "try:\n    pass\nfinally:\n    def f(): pass"
        assert _names(src) == ["f"]

    def test_definition_in_else_branch_is_collected(self):
        src = "if x:\n    pass\nelse:\n    def f(): pass"
        assert _names(src) == ["f"]

    def test_definition_in_match_case_is_collected(self):
        src = "match cmd:\n    case 1:\n        def f(): pass"
        assert _names(src) == ["f"]


class TestEnclosingClass:
    """
    `class_name` answers a different question from the symbol id: what type
    does `self` refer to. It must stay the bare enclosing class so attribute
    resolution keeps working, which is why it is derived from the qualname
    rather than being the qualname.
    """

    @pytest.mark.parametrize(
        "qualname,expected",
        [
            ("f", None),
            ("A.f", "A"),
            ("A.B.f", "A.B"),
            ("A.f.<locals>.g", "A"),
            ("f.<locals>.g", None),
            ("f.<locals>.B.g", "B"),
            ("A.f.<locals>.B.g", "A.B"),
        ],
    )
    def test_table(self, qualname, expected):
        assert _enclosing_class(qualname) == expected

    def test_nested_function_keeps_enclosing_class(self):
        """
        A closure inside a method can legally close over `self`, so it keeps
        the enclosing class. Dropping it would lose those attribute edges.
        """
        assert _enclosing_class("A.f.<locals>.g") == "A"

    def test_matches_legacy_class_stack_semantics(self):
        """
        The pre-refactor collector computed class_name as ".".join(class_stack)
        over ClassDef nodes only. _enclosing_class must reproduce that exactly
        — the refactor was meant to change ids, not attribute resolution.
        """
        src = (
            "class A:\n"
            "    class B:\n"
            "        def f(self):\n"
            "            def g(): pass\n"
            "    def h(self):\n"
            "        class C:\n"
            "            def i(self): pass\n"
            "def top():\n"
            "    def inner(): pass\n"
        )
        tree = ast.parse(src)

        legacy = {}

        def walk(stmts, class_stack):
            for node in stmts:
                if isinstance(node, ast.ClassDef):
                    class_stack.append(node.name)
                    walk(node.body, class_stack)
                    class_stack.pop()
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    legacy[id(node)] = ".".join(class_stack) if class_stack else None
                    walk(node.body, class_stack)

        walk(tree.body, [])

        for qualname, node in collect_functions(tree):
            assert _enclosing_class(qualname) == legacy[id(node)], qualname


class TestParserGraphAgreement:
    """
    The drift this refactor removes: graph_builder used to walk the AST
    itself, and its copy missed conditional blocks. Every id the parser
    emits must have a node in the graph, or retrieval scores a symbol that
    has no edges to reach it.
    """

    def _write_repo(self, tmp_path):
        (tmp_path / "mod.py").write_text(
            "from typing import TYPE_CHECKING\n"
            "\n"
            "if TYPE_CHECKING:\n"
            "    def only_when_typing():\n"
            "        return 1\n"
            "\n"
            "try:\n"
            "    import json\n"
            "except ImportError:\n"
            "    def fallback():\n"
            "        return 2\n"
            "\n"
            "class A:\n"
            "    def one(self):\n"
            "        def decorator():\n"
            "            return self.helper()\n"
            "        return decorator\n"
            "\n"
            "    def two(self):\n"
            "        def decorator():\n"
            "            return 4\n"
            "        return decorator\n"
            "\n"
            "    def helper(self):\n"
            "        return 5\n"
        )
        return str(tmp_path)

    def test_every_parsed_symbol_has_a_graph_node(self, tmp_path):
        repo = self._write_repo(tmp_path)
        symbols = extract_all_symbols(repo)
        graph = build_repository_graph(repo)

        missing = sorted(set(symbols) - set(graph))
        assert not missing, f"symbols with no graph node: {missing}"

    def test_conditional_definitions_reach_the_graph(self, tmp_path):
        repo = self._write_repo(tmp_path)
        graph = build_repository_graph(repo)

        assert "./mod.py:only_when_typing" in graph
        assert "./mod.py:fallback" in graph

    def test_sibling_closures_are_separate_graph_nodes(self, tmp_path):
        repo = self._write_repo(tmp_path)
        graph = build_repository_graph(repo)

        assert "./mod.py:A.one.<locals>.decorator" in graph
        assert "./mod.py:A.two.<locals>.decorator" in graph

    def test_closure_keeps_self_attribute_edge(self, tmp_path):
        """
        `decorator` closes over `self`, so class_name must survive into the
        nested scope for `self.helper()` to resolve. This is the edge that a
        naive "strip everything after <locals>" would silently drop.
        """
        repo = self._write_repo(tmp_path)
        graph = build_repository_graph(repo)

        assert "./mod.py:A.helper" in graph["./mod.py:A.one.<locals>.decorator"]
