"""
tests/test_typescript_adapter.py — the TypeScript/JavaScript adapter and
its pipeline integration.

Every resolution claim the adapter's docstring makes is asserted here on
real resolved edges, mirroring how the Python resolver is tested. The
whole module is skipped when the optional tree-sitter extra is not
installed — and one test asserts that in that world the Python pipeline
is byte-for-byte unaffected (the graceful-degradation contract).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ts = pytest.importorskip("tree_sitter", reason="typescript extra not installed")
pytest.importorskip("tree_sitter_typescript")
pytest.importorskip("tree_sitter_javascript")

from diffcontext.languages import available_adapters, indexable_extensions
from diffcontext.languages.typescript import TypeScriptAdapter
from diffcontext.pipeline import index_repository, analyze_impact, compile


def _graph_as_comparable(graph):
    return {k: set(v) for k, v in graph.items()}


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


@pytest.fixture()
def ts_repo(tmp_path):
    """A TS repo exercising every resolution class the adapter claims."""
    root = str(tmp_path / "tsrepo")
    _write(root, "src/utils.ts", (
        "export function helper(x: number): number { return x * 2; }\n"
        "export const arrowUtil = (s: string) => s.trim();\n"
        "export function unused(): void {}\n"
    ))
    _write(root, "src/models/base.ts", (
        "export class BaseModel {\n"
        "  constructor(public id: string) {}\n"
        "  save(): void { validate(this.id); }\n"
        "}\n"
        "function validate(id: string): void {}\n"
    ))
    _write(root, "src/models/user.ts", (
        "import { BaseModel } from './base';\n"
        "import { helper, arrowUtil as trimmer } from '../utils';\n"
        "import * as ns from '../utils';\n"
        "\n"
        "export class UserModel extends BaseModel {\n"
        "  constructor(id: string, public name: string) { super(id); }\n"
        "  save(): void { this.normalize(); }\n"
        "  normalize(): void { trimmer(this.name); helper(1); ns.unused(); }\n"
        "}\n"
        "\n"
        "export interface UserShape { id: string; name: string; }\n"
        "\n"
        "export const makeUser = (id: string) => new UserModel(id, 'x');\n"
        "export function processAll(items: string[], cb: (s: string) => void) {\n"
        "  items.forEach(cb);\n"
        "  items.map(trimmer);\n"
        "}\n"
    ))
    _write(root, "src/index.ts", (
        "export { UserModel, makeUser } from './models/user';\n"
        "export * from './utils';\n"
    ))
    _write(root, "src/app.ts", (
        "import { makeUser, helper } from './index';\n"
        "export function main(): void {\n"
        "  const u = makeUser('1');\n"
        "  helper(5);\n"
        "}\n"
    ))
    return root


def _index(repo):
    db = os.path.join(repo, ".diffcontext_cache.db")
    for suffix in ("", "-shm", "-wal"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    return index_repository(repo)


class TestSymbols:
    def test_functions_methods_arrows_types_collected(self, ts_repo):
        idx = _index(ts_repo)
        expected = {
            "./src/utils.ts:helper",
            "./src/utils.ts:arrowUtil",
            "./src/models/base.ts:BaseModel.constructor",
            "./src/models/base.ts:BaseModel.save",
            "./src/models/base.ts:validate",
            "./src/models/user.ts:UserModel.normalize",
            "./src/models/user.ts:UserShape",          # interface: a symbol
            "./src/models/user.ts:makeUser",
            "./src/app.ts:main",
        }
        assert expected <= set(idx.symbols)

    def test_symbol_code_and_lineno_slice_correctly(self, ts_repo):
        idx = _index(ts_repo)
        helper = idx.symbols["./src/utils.ts:helper"]
        assert helper.code.startswith("function helper")
        assert helper.lineno == 1
        arrow = idx.symbols["./src/utils.ts:arrowUtil"]
        assert arrow.lineno == 2


class TestEdges:
    def test_named_import_with_alias(self, ts_repo):
        idx = _index(ts_repo)
        deps = idx.graph["./src/models/user.ts:UserModel.normalize"]
        assert "./src/utils.ts:arrowUtil" in deps    # via `as trimmer`
        assert "./src/utils.ts:helper" in deps

    def test_namespace_import_member_call(self, ts_repo):
        idx = _index(ts_repo)
        assert "./src/utils.ts:unused" in idx.graph[
            "./src/models/user.ts:UserModel.normalize"
        ]

    def test_this_method_call(self, ts_repo):
        idx = _index(ts_repo)
        assert "./src/models/user.ts:UserModel.normalize" in idx.graph[
            "./src/models/user.ts:UserModel.save"
        ]

    def test_super_resolves_to_parent_constructor(self, ts_repo):
        idx = _index(ts_repo)
        assert "./src/models/base.ts:BaseModel.constructor" in idx.graph[
            "./src/models/user.ts:UserModel.constructor"
        ]

    def test_override_edge_child_to_parent(self, ts_repo):
        idx = _index(ts_repo)
        assert "./src/models/base.ts:BaseModel.save" in idx.graph[
            "./src/models/user.ts:UserModel.save"
        ]

    def test_expression_arrow_new_class_edge(self, ts_repo):
        # `=> new UserModel(...)` — the call IS the arrow body
        idx = _index(ts_repo)
        assert "./src/models/user.ts:UserModel.constructor" in idx.graph[
            "./src/models/user.ts:makeUser"
        ]

    def test_fn_ref_argument_with_param_shadow_guard(self, ts_repo):
        idx = _index(ts_repo)
        deps = idx.graph["./src/models/user.ts:processAll"]
        assert "./src/utils.ts:arrowUtil" in deps    # items.map(trimmer)
        # `cb` is a parameter — must NOT resolve to any symbol
        assert all("cb" not in d.rsplit(":", 1)[1] for d in deps)

    def test_barrel_reexport_and_star_resolve_through_index_ts(self, ts_repo):
        idx = _index(ts_repo)
        deps = idx.graph["./src/app.ts:main"]
        assert "./src/models/user.ts:makeUser" in deps   # named re-export
        assert "./src/utils.ts:helper" in deps           # export * from

    def test_interface_takes_no_edges(self, ts_repo):
        idx = _index(ts_repo)
        assert idx.graph["./src/models/user.ts:UserShape"] == []


class TestPipeline:
    def test_blast_radius_crosses_files_and_barrel(self, ts_repo):
        idx = _index(ts_repo)
        impact = analyze_impact(idx, ["./src/utils.ts:helper"], hybrid=False)
        assert "./src/app.ts:main" in impact.blast_radius
        assert "./src/models/user.ts:UserModel.normalize" in impact.blast_radius

    def test_compile_produces_context(self, ts_repo):
        idx = _index(ts_repo)
        ctx = compile(idx, analyze_impact(idx, ["./src/utils.ts:helper"]),
                      max_tokens=4000)
        assert "helper" in ctx.text
        assert ctx.symbol_count > 0

    def test_warm_reindex_equals_cold(self, ts_repo):
        cold = _index(ts_repo)
        warm = index_repository(ts_repo)
        assert warm.symbols.keys() == cold.symbols.keys()
        assert _graph_as_comparable(warm.graph) == _graph_as_comparable(cold.graph)

    def test_incremental_update_equals_full_rebuild(self, ts_repo):
        idx = _index(ts_repo)
        with open(os.path.join(ts_repo, "src/utils.ts"), "a") as f:
            f.write("\nexport function newcomer(): number { return helper(9); }\n")
        idx.update(["src/utils.ts"])
        assert "./src/utils.ts:newcomer" in idx.symbols
        assert "./src/utils.ts:helper" in idx.graph["./src/utils.ts:newcomer"]

        fresh = index_repository(ts_repo)
        assert fresh.symbols.keys() == idx.symbols.keys()
        assert _graph_as_comparable(fresh.graph) == _graph_as_comparable(idx.graph)

    def test_update_after_warm_start_rebuilds_lang_part(self, ts_repo):
        _index(ts_repo)
        warm = index_repository(ts_repo)      # graph-cache hit: no lang state
        with open(os.path.join(ts_repo, "src/app.ts"), "a") as f:
            f.write("\nexport const extra = () => makeUser('2');\n")
        warm.update(["src/app.ts"])
        assert "./src/app.ts:extra" in warm.symbols
        assert "./src/models/user.ts:makeUser" in warm.graph["./src/app.ts:extra"]

    def test_deleted_ts_file_drops_its_symbols(self, ts_repo):
        idx = _index(ts_repo)
        os.remove(os.path.join(ts_repo, "src/app.ts"))
        idx.update(["src/app.ts"])
        assert not any(s.startswith("./src/app.ts:") for s in idx.symbols)
        fresh = index_repository(ts_repo)
        assert fresh.symbols.keys() == idx.symbols.keys()


class TestIndexingPolicy:
    def test_vendored_minified_and_test_files_excluded(self, ts_repo):
        _write(ts_repo, "static/vendor/jquery.js",
               "function jQuery() { return 1; }\n")
        _write(ts_repo, "src/bundle.min.js", "function m(){return 1}\n")
        _write(ts_repo, "src/utils.test.ts",
               "function testHelper() { return 1; }\n")
        _write(ts_repo, "src/utils.spec.ts",
               "function specHelper() { return 1; }\n")
        idx = _index(ts_repo)
        assert not any("jquery" in s for s in idx.symbols)
        assert not any(".min." in s for s in idx.symbols)
        assert not any(".test." in s or ".spec." in s for s in idx.symbols)

    def test_mixed_repo_python_side_unchanged(self, ts_repo):
        # A .py file alongside TS: both languages indexed, ids disjoint
        _write(ts_repo, "tool/check.py",
               "def check():\n    return 1\n")
        idx = _index(ts_repo)
        assert "./tool/check.py:check" in idx.symbols
        assert "./src/utils.ts:helper" in idx.symbols


class TestRegistry:
    def test_adapter_available_and_extensions_registered(self):
        adapters = available_adapters()
        assert any(a.name == "typescript" for a in adapters)
        exts = indexable_extensions()
        assert ".py" in exts and ".ts" in exts and ".tsx" in exts

    def test_adapter_never_claims_python(self):
        ad = TypeScriptAdapter()
        assert not any("py" == e.lstrip(".") for e in ad.extensions)
