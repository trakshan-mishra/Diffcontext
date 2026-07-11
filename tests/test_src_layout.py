#!/usr/bin/env python3
"""
tests/test_src_layout.py — Regression tests for src-layout import resolution.

Found on psf/black (2026-07): for `src/blackd/__init__.py`, the resolver
produced an import map with a single entry — every import of the `black`
package failed because absolute imports were only resolved against the
repository root, while the packages live under `src/` (the standard
setuptools src-layout used by black, flask, and most modern PyPI projects).
As a result the call graph had NO edge for
`blackd.format_code → black.format_file_contents`, silently truncating
blast radii one hop before the actual code under change.

The fixture at tests/fixtures/src_layout_repo reproduces that shape:
`src/myservice/app.py` does `import mypkg` / `import mypkg.core` and calls
through module attributes, while `src/mypkg/__init__.py` re-exports
implementation functions both absolutely and relatively.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.resolver import build_import_map
from diffcontext.graph_builder import build_repository_graph

BASE = os.path.dirname(os.path.dirname(__file__))
SRC_LAYOUT = os.path.join(BASE, "tests", "fixtures", "src_layout_repo")

APP = "./src/myservice/app.py"
CONSUMER = "./src/myservice/consumer.py"
PKG_INIT = "./src/mypkg/__init__.py"
CORE = "./src/mypkg/core.py"
HELPERS = "./src/mypkg/helpers.py"


@pytest.fixture(scope="module")
def graph():
    assert os.path.isdir(SRC_LAYOUT), f"fixture missing at {SRC_LAYOUT}"
    return build_repository_graph(SRC_LAYOUT)


class TestSrcLayoutImportMap:
    def test_bare_import_resolves_under_src(self):
        m = build_import_map(
            os.path.join(SRC_LAYOUT, "src", "myservice", "app.py"), SRC_LAYOUT
        )
        assert "mypkg" in m, (
            "`import mypkg` must resolve when the package lives under src/ "
            "(the psf/black `import black` case)"
        )
        assert m["mypkg"].endswith(os.path.join("mypkg", "__init__.py"))

    def test_dotted_import_binds_package_not_submodule(self):
        # `import mypkg.core` makes BOTH `mypkg` and `mypkg.core` usable.
        # The local name `mypkg` must point at the package __init__, not at
        # core.py (the previous behavior bound the top name to the submodule,
        # so `mypkg.other_fn()` resolved into the wrong file).
        m = build_import_map(
            os.path.join(SRC_LAYOUT, "src", "myservice", "app.py"), SRC_LAYOUT
        )
        assert m["mypkg"].endswith(os.path.join("mypkg", "__init__.py"))
        assert "mypkg.core" in m
        assert m["mypkg.core"].endswith(os.path.join("mypkg", "core.py"))

    def test_from_import_resolves_under_src(self):
        m = build_import_map(
            os.path.join(SRC_LAYOUT, "src", "myservice", "consumer.py"), SRC_LAYOUT
        )
        # from mypkg import top_fn → defined in __init__ itself
        assert "top_fn" in m
        assert m["top_fn"].endswith(os.path.join("mypkg", "__init__.py"))
        # from mypkg.core import core_fn as core_direct
        assert "core_direct" in m
        assert m["core_direct"].endswith(os.path.join("mypkg", "core.py"))

    def test_from_import_follows_absolute_reexport(self):
        # `from mypkg import core_fn` — core_fn only exists in __init__.py as
        # an ABSOLUTE re-export (`from mypkg.core import core_fn`), the same
        # shape as black's `from black.parsing import ...` re-exports.
        m = build_import_map(
            os.path.join(SRC_LAYOUT, "src", "myservice", "consumer.py"), SRC_LAYOUT
        )
        assert "core_fn" in m
        assert m["core_fn"].endswith(os.path.join("mypkg", "core.py"))


class TestSrcLayoutGraphEdges:
    def test_bare_module_attr_call(self, graph):
        # mypkg.top_fn() — the exact `black.format_file_contents(...)` pattern
        assert f"{PKG_INIT}:top_fn" in graph.get(f"{APP}:use_top", [])

    def test_module_attr_call_through_absolute_reexport(self, graph):
        assert f"{CORE}:core_fn" in graph.get(f"{APP}:use_absolute_reexport", [])

    def test_module_attr_call_through_relative_reexport(self, graph):
        assert f"{HELPERS}:helper_fn" in graph.get(
            f"{APP}:use_relative_reexport", []
        )

    def test_dotted_module_attr_call(self, graph):
        # mypkg.core.core_fn() — attribute call through a dotted module import
        assert f"{CORE}:core_fn" in graph.get(f"{APP}:use_dotted_module_call", [])

    def test_from_import_edges(self, graph):
        deps = graph.get(f"{CONSUMER}:call_from_import", [])
        assert f"{PKG_INIT}:top_fn" in deps
        assert f"{CORE}:core_fn" in deps

    def test_no_wrong_file_edges_from_dotted_binding(self, graph):
        # Regression guard for the `import a.b` binding bug: `mypkg.top_fn()`
        # must NOT resolve into core.py just because `import mypkg.core`
        # appears in the same file.
        assert f"{CORE}:top_fn" not in graph.get(f"{APP}:use_top", [])


class TestFunctionReferenceArguments:
    """Function references passed as call arguments are real dependencies.

    Found on psf/black: blackd's format_code never *calls*
    black.format_file_contents — it passes it to functools.partial. A
    call-only graph drops that edge and truncates the blast radius one hop
    before the code under change.
    """

    def test_module_attr_ref_as_argument(self, graph):
        # run(mypkg.core_fn, ...) — module-attribute function reference
        assert f"{CORE}:core_fn" in graph.get(
            f"{APP}:use_function_ref_as_argument", []
        )

    def test_keyword_function_ref(self, graph):
        # sorted(records, key=mypkg.top_fn)
        assert f"{PKG_INIT}:top_fn" in graph.get(
            f"{APP}:use_keyword_function_ref", []
        )

    def test_local_function_ref_as_argument(self, graph):
        assert f"{APP}:local_fn" in graph.get(f"{APP}:use_local_function_ref", [])

    def test_parameter_shadowing_creates_no_edge(self, graph):
        # `local_fn` is a parameter of this function; passing it along must
        # not be attributed to the module-level function of the same name.
        assert f"{APP}:local_fn" not in graph.get(
            f"{APP}:no_edge_for_shadowing_param", []
        )
