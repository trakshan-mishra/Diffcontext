"""
test_graph_resolution.py — Regression tests for real call-graph resolution
bugs found and fixed during manual testing.

Unlike test_core.py's TestGraphBuilder, these build tiny repos on the fly
using pytest's tmp_path fixture -- no external clone or fixture directory
required, so they actually run instead of skipping.

Each test corresponds to a specific bug found during adversarial testing:

- test_free_function_local_var_resolves: a free function (not a class
  method) doing `x = SomeClass(); x.method()` previously resolved to zero
  call-graph edges, because attribute/type tracking only ever looked
  inside ast.ClassDef bodies. Fixed via symbols.extract_local_var_types.

- test_import_alias_resolves: `from .mod import RealName as Alias` then
  `Alias().method()` previously failed to resolve, because class_registry
  is keyed by the real class name, never the import alias, and ast only
  exposes the alias at the call site. Fixed via graph_builder's
  classes_by_file reverse index.

- test_import_alias_with_naming_collision: same as above, but with two
  *different* classes sharing the same name in different files, each
  aliased differently on import -- confirms the fix disambiguates
  correctly rather than just getting lucky with a single-file case.
"""

import os
import subprocess

import pytest

from diffcontext.pipeline import index_repository


def _git_init(repo_dir):
    """Minimal git init so the repo is valid for tools that expect one."""
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)


class TestFreeFunctionLocalVarTracking:
    """
    Regression test for: free functions instantiating a class in a local
    variable and calling a method on it produced zero resolved edges.
    """

    def test_free_function_local_var_resolves(self, tmp_path):
        (tmp_path / "user.py").write_text(
            "class Handler:\n"
            "    def process(self):\n"
            "        return self._do_work()\n"
            "\n"
            "    def _do_work(self):\n"
            "        return 'work'\n"
        )
        (tmp_path / "caller.py").write_text(
            "from .user import Handler\n"
            "\n"
            "def run():\n"
            "    h = Handler()\n"
            "    return h.process()\n"
        )
        (tmp_path / "__init__.py").write_text("")

        idx = index_repository(str(tmp_path))

        run_edges = idx.graph.get("./caller.py:run", [])
        assert "./user.py:Handler.process" in run_edges, (
            f"Expected run() to resolve its call to Handler.process(), "
            f"got edges: {run_edges}"
        )

    def test_free_function_with_annotated_param_resolves(self, tmp_path):
        """Same bug, but via a typed parameter instead of `Class()` directly."""
        (tmp_path / "user.py").write_text(
            "class Handler:\n"
            "    def process(self):\n"
            "        return 1\n"
        )
        (tmp_path / "caller.py").write_text(
            "from .user import Handler\n"
            "\n"
            "def run(h: Handler):\n"
            "    return h.process()\n"
        )
        (tmp_path / "__init__.py").write_text("")

        idx = index_repository(str(tmp_path))

        run_edges = idx.graph.get("./caller.py:run", [])
        assert "./user.py:Handler.process" in run_edges


class TestImportAliasResolution:
    """
    Regression test for: `from .mod import RealName as Alias` then
    `Alias().method()` failed to resolve because class_registry is keyed
    by the real class name, not the alias used at the call site.
    """

    def test_import_alias_resolves(self, tmp_path):
        (tmp_path / "user.py").write_text(
            "class Handler:\n"
            "    def process(self):\n"
            "        return 1\n"
        )
        (tmp_path / "caller.py").write_text(
            "from .user import Handler as UserHandler\n"
            "\n"
            "def run():\n"
            "    u = UserHandler()\n"
            "    return u.process()\n"
        )
        (tmp_path / "__init__.py").write_text("")

        idx = index_repository(str(tmp_path))

        run_edges = idx.graph.get("./caller.py:run", [])
        assert "./user.py:Handler.process" in run_edges, (
            f"Expected aliased import to resolve to the real class's method, "
            f"got edges: {run_edges}"
        )

    def test_import_alias_with_naming_collision(self, tmp_path):
        """
        Two different classes, both named 'Handler', in different files,
        each aliased differently on import. Confirms the fix disambiguates
        by target file rather than just matching on name.
        """
        (tmp_path / "user.py").write_text(
            "class Handler:\n"
            "    def process(self):\n"
            "        return 'user work'\n"
        )
        (tmp_path / "order.py").write_text(
            "class Handler:\n"
            "    def process(self):\n"
            "        return 'order work'\n"
        )
        (tmp_path / "caller.py").write_text(
            "from .user import Handler as UserHandler\n"
            "from .order import Handler as OrderHandler\n"
            "\n"
            "def run_both():\n"
            "    u = UserHandler()\n"
            "    o = OrderHandler()\n"
            "    return u.process(), o.process()\n"
        )
        (tmp_path / "__init__.py").write_text("")

        idx = index_repository(str(tmp_path))

        edges = idx.graph.get("./caller.py:run_both", [])
        assert "./user.py:Handler.process" in edges, (
            f"Expected UserHandler alias to resolve to user.py's Handler, "
            f"got edges: {edges}"
        )
        assert "./order.py:Handler.process" in edges, (
            f"Expected OrderHandler alias to resolve to order.py's Handler, "
            f"got edges: {edges}"
        )


class TestKnownLimitations:
    """
    Tests that document KNOWN, accepted limitations rather than bugs --
    these assert the current (incomplete) behavior so a future change that
    silently "fixes" or further breaks them is visible in the diff, not
    just discovered by accident.
    """

    def test_decorator_wrapped_function_call_not_attributed_to_wrapper(self, tmp_path):
        """
        Known limitation: a nested function's calls get attributed to the
        enclosing function in the graph, not tracked as the nested
        function's own node. This means a decorated function's real
        dependency (via its wrapper) is invisible to blast radius.
        Documented in conversation; not fixed as of this test.
        """
        (tmp_path / "app.py").write_text(
            "def require_auth(func):\n"
            "    def wrapper(*args, **kwargs):\n"
            "        if not check_session():\n"
            "            return 'denied'\n"
            "        return func(*args, **kwargs)\n"
            "    return wrapper\n"
            "\n"
            "def check_session():\n"
            "    return True\n"
            "\n"
            "@require_auth\n"
            "def get_profile(user_id):\n"
            "    return user_id\n"
        )
        (tmp_path / "__init__.py").write_text("")

        idx = index_repository(str(tmp_path))

        # KNOWN LIMITATION: get_profile shows no edge to require_auth/wrapper/
        # check_session, even though at runtime the decorator wires them
        # together. If this assertion ever starts failing, it likely means
        # decorator resolution was improved -- update this test, don't just
        # delete it.
        get_profile_edges = idx.graph.get("./app.py:get_profile", [])
        assert get_profile_edges == [], (
            "If this fails, decorator call attribution may have improved -- "
            "update this test to assert the new, better behavior instead "
            "of deleting it."
        )

    def test_higher_order_function_arg_not_tracked_as_call(self, tmp_path):
        """
        Known limitation: passing a function BY REFERENCE to a higher-order
        function (map, sorted(key=...), etc.) is not detected as a call,
        since there's no ast.Call node for the passed function itself.
        """
        (tmp_path / "funcs.py").write_text(
            "def with_map(items):\n"
            "    return list(map(formatter, items))\n"
            "\n"
            "def formatter(x):\n"
            "    return str(x)\n"
        )
        (tmp_path / "__init__.py").write_text("")

        idx = index_repository(str(tmp_path))

        with_map_edges = idx.graph.get("./funcs.py:with_map", [])
        assert "./funcs.py:formatter" not in with_map_edges, (
            "If this fails, higher-order function argument tracking may "
            "have improved -- update this test to assert the new behavior."
        )


class TestBareImportResolution:
    """
    Regression test for: `import store` (bare, no dots, no `from`) failed
    to resolve when store.py was nested in a subdirectory rather than
    sitting at the repo root -- a very common pattern in script-style
    codebases where sibling scripts do `import other_script` and rely on
    their own directory being on sys.path at runtime.

    Found via a real user repo: watchlist.py did `import store` where
    store.py lived 3 directories deep, and every store.update_run() call
    site silently produced zero resolved edges.
    """

    def test_bare_import_resolves_from_nested_sibling_directory(self, tmp_path):
        scripts_dir = tmp_path / "skills" / "last30days" / "scripts"
        scripts_dir.mkdir(parents=True)

        (scripts_dir / "store.py").write_text(
            "def update_run(run_id, **kwargs):\n"
            "    return run_id\n"
        )
        (scripts_dir / "watchlist.py").write_text(
            "import store\n"
            "\n"
            "def do_something(run_id):\n"
            "    store.update_run(run_id, status='done')\n"
        )
        (tmp_path / "__init__.py").write_text("")
        (scripts_dir / "__init__.py").write_text("")

        idx = index_repository(str(tmp_path))

        edges = idx.graph.get("./skills/last30days/scripts/watchlist.py:do_something", [])
        assert "./skills/last30days/scripts/store.py:update_run" in edges, (
            f"Expected bare 'import store' to resolve via sibling-directory "
            f"fallback, got edges: {edges}"
        )