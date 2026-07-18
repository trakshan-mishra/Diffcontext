"""
typescript.py — TypeScript / JavaScript adapter built on tree-sitter.

Supplies the two things the language-agnostic pipeline needs (see
languages/__init__.py): per-file symbols and a dependency graph.

What it resolves (asserted by tests/test_typescript_adapter.py):
  - function declarations, class methods (incl. static/async/generators),
    const/let/var arrow-function and function-expression bindings,
    namespace members ("Ns.fn" ids), enums, interfaces and type aliases
    (as retrievable context symbols; they take no call edges)
  - ES imports: named (with aliases), default, and namespace imports,
    resolved through relative specifiers, index files (barrel re-export
    following, `export {X} from './y'` and `export * from`, depth-capped),
    and the ESM ".js"-suffix-means-".ts" convention
  - call edges: bare calls, `this.method()`, namespace-member calls,
    `new Class()` → Class.constructor, `super()` → parent constructor,
    child→parent method override edges via `extends`, and function
    references passed as call arguments (parameter-shadowing guarded)

What it deliberately does NOT do (v1, disclosed): no type inference —
`obj.method()` on an arbitrary object is unresolved; no tsconfig path
aliases (`@/utils`); no CommonJS `require()`. These lower graph
confidence, which the meta header reports per-package as always.
"""

import json
import logging
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from ..models import Symbol

logger = logging.getLogger(__name__)

# Imported at module load so languages/__init__ availability probing fails
# fast when the optional extras are missing.
from tree_sitter import Language, Parser
import tree_sitter_typescript as _ts_grammar
import tree_sitter_javascript as _js_grammar

_LANG_TS = Language(_ts_grammar.language_typescript())
_LANG_TSX = Language(_ts_grammar.language_tsx())
_LANG_JS = Language(_js_grammar.language())

_FUNCTION_DECLS = ("function_declaration", "generator_function_declaration")
_TYPE_DECLS = ("interface_declaration", "type_alias_declaration", "enum_declaration")
_CLASS_DECLS = ("class_declaration", "abstract_class_declaration")
_FUNCTION_VALUES = ("arrow_function", "function_expression", "function")
# Statement nodes whose blocks can contain further definitions.
_DESCEND_STMTS = (
    "statement_block", "if_statement", "for_statement", "for_in_statement",
    "while_statement", "do_statement", "try_statement", "switch_statement",
    "labeled_statement", "ambient_declaration",
)


class _FileFacts:
    """Everything the graph builder needs about one parsed file."""

    __slots__ = (
        "symbols", "class_nodes", "class_methods", "class_fields",
        "reexports", "tree",
    )

    def __init__(self, tree):
        self.tree = tree
        # [(qualified_name, node)] for function/method/type symbols
        self.symbols: List[Tuple[str, object]] = []
        # qualified class name -> class node (for extends resolution)
        self.class_nodes: Dict[str, object] = {}
        # qualified class name -> set of method names
        self.class_methods: Dict[str, Set[str]] = {}
        # qualified class name -> {field name -> declared type name}, from
        # typed field declarations and constructor parameter properties
        # (`constructor(private db: Database)`) — the receiver types that
        # make `this.db.query()` resolvable.
        self.class_fields: Dict[str, Dict[str, str]] = {}
        # ({exported: (specifier, original)}, [star specifiers])
        self.reexports: Tuple[Dict[str, Tuple[str, str]], List[str]] = ({}, [])


class TypeScriptAdapter:
    name = "typescript"
    extensions = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

    # Resolution candidates for an extensionless import specifier, in
    # Node/bundler probe order.
    _RESOLVE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

    # Directories whose JS/TS is not project source: build output, vendored
    # libraries, and web assets. Measured hazard, not hypothetical: without
    # this, indexing django pulls in its tracked admin static JS — jquery
    # included — polluting a Python repo's index with 47 vendor symbols.
    # Deliberately adapter-scoped, NOT in scanner.EXCLUDED_DIRS: a Python
    # package named `static/` must keep being indexed.
    _EXCLUDED_DIR_PARTS = {
        "static", "staticfiles", "assets", "public", "vendor", "vendors",
        "coverage", ".next", ".nuxt", "out",
    }

    def should_index(self, path: str) -> bool:
        """Indexing policy for a discovered file of this language."""
        base = os.path.basename(path).lower()
        if ".min." in base:
            return False  # minified bundles: one unreadable megasymbol
        # Colocated test files (foo.test.ts / foo.spec.ts) — same policy
        # as the scanner's tests/ dir exclusion.
        stem = base
        for ext in self.extensions:
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        if stem.endswith((".test", ".spec")):
            return False
        parts = path.replace(os.sep, "/").lower().split("/")[:-1]
        return not any(p in self._EXCLUDED_DIR_PARTS for p in parts)

    def _parse(self, path: str, source: str):
        if path.endswith(".tsx"):
            lang = _LANG_TSX
        elif path.endswith(".ts"):
            lang = _LANG_TS
        else:
            lang = _LANG_JS
        return Parser(lang).parse(source.encode("utf-8"))

    # ── Symbol extraction (pipeline cache entry point) ───────────────────

    def extract_file_symbols(
        self, filename: str, repo_path: str, source: str
    ) -> Dict[str, Symbol]:
        """Symbols for one file: id "./rel/path.ts:Container.name"."""
        rel = "./" + os.path.relpath(filename, repo_path)
        facts = _gather_facts(self._parse(filename, source))
        raw = source.encode("utf-8")
        symbols: Dict[str, Symbol] = {}
        for name, node in facts.symbols:
            sym_id = f"{rel}:{name}"
            code = raw[node.start_byte : node.end_byte].decode("utf-8", "ignore")
            symbols[sym_id] = Symbol(
                id=sym_id,
                file=filename,
                name=name,
                code=code,
                lineno=node.start_point[0] + 1,
            )
        return symbols

    # ── Graph construction ───────────────────────────────────────────────

    def build_language_graph(
        self, repo_path: str, sources: Dict[str, str]
    ) -> Dict[str, List[str]]:
        """
        Dependency graph over this language's symbols. `sources` maps
        "./rel/path.ts" -> file text; every file is parsed exactly once.
        """
        repo_abs = os.path.abspath(repo_path)
        facts: Dict[str, _FileFacts] = {
            rel: _gather_facts(self._parse(rel, text))
            for rel, text in sources.items()
        }

        def probe(target: str) -> Optional[str]:
            """Abs path guess -> "./rel" of an indexed file, trying the
            extension/index-file conventions."""
            candidates = [target]
            root, ext = os.path.splitext(target)
            # ESM convention: `import ... from './x.js'` refers to x.ts on disk
            if ext in (".js", ".mjs", ".cjs"):
                candidates += [root + ".ts", root + ".tsx"]
            if ext == "" or ext not in self._RESOLVE_EXTS:
                candidates += [target + e for e in self._RESOLVE_EXTS]
                candidates += [
                    os.path.join(target, "index" + e) for e in self._RESOLVE_EXTS
                ]
            for cand in candidates:
                rel_cand = "./" + os.path.relpath(cand, repo_abs)
                if rel_cand in facts:
                    return rel_cand
            return None

        tsconfig_cache: Dict[str, Optional[Tuple[str, str, Dict[str, List[str]]]]] = {}

        def nearest_tsconfig(dir_abs: str):
            """(config_dir, baseUrl, paths) from the nearest tsconfig.json /
            jsconfig.json at or above dir_abs (stopping at the repo root)."""
            if dir_abs in tsconfig_cache:
                return tsconfig_cache[dir_abs]
            result = None
            cur = dir_abs
            while True:
                for name in ("tsconfig.json", "jsconfig.json"):
                    cfg_path = os.path.join(cur, name)
                    if os.path.isfile(cfg_path):
                        result = _load_tsconfig(cfg_path)
                        break
                if result is not None or cur == repo_abs or len(cur) <= len(repo_abs):
                    break
                cur = os.path.dirname(cur)
            tsconfig_cache[dir_abs] = result
            return result

        def resolve_specifier(importing_rel: str, spec: str) -> Optional[str]:
            """'./x', '@/x' (tsconfig paths), or baseUrl-relative specifier
            -> "./resolved/x.ts" rel; None for external packages."""
            base_dir = os.path.dirname(os.path.join(repo_abs, importing_rel[2:]))
            if spec.startswith("."):
                return probe(os.path.normpath(os.path.join(base_dir, spec)))

            cfg = nearest_tsconfig(base_dir)
            if cfg is None:
                return None
            cfg_dir, base_url, paths = cfg
            base_abs = os.path.normpath(os.path.join(cfg_dir, base_url))
            for pattern, targets in paths.items():
                if pattern.endswith("*"):
                    prefix = pattern[:-1]
                    if not spec.startswith(prefix):
                        continue
                    star = spec[len(prefix):]
                elif spec == pattern:
                    star = ""
                else:
                    continue
                for target in targets:
                    resolved = probe(os.path.normpath(os.path.join(
                        base_abs, target.replace("*", star)
                    )))
                    if resolved:
                        return resolved
            # Bare specifier via baseUrl (`import x from 'utils/x'` with
            # baseUrl=./src). Only ever hits files we indexed, so real npm
            # packages can't be mis-resolved.
            if base_url:
                return probe(os.path.normpath(os.path.join(base_abs, spec)))
            return None

        def defined_names(file_rel: str) -> Set[str]:
            f = facts.get(file_rel)
            if f is None:
                return set()
            return {n for n, _node in f.symbols} | set(f.class_nodes)

        def follow_barrel(file_rel: str, name: str, _depth: int = 0) -> Tuple[str, str]:
            """If file re-exports `name` from elsewhere, return the real
            (file, name); else (file_rel, name). Depth-capped like the
            Python __init__.py transparency."""
            if _depth > 2 or file_rel not in facts:
                return file_rel, name
            named, stars = facts[file_rel].reexports
            if name in named:
                spec, orig = named[name]
                target = resolve_specifier(file_rel, spec)
                if target:
                    return follow_barrel(target, orig, _depth + 1)
            if name in defined_names(file_rel):
                return file_rel, name
            for spec in stars:
                target = resolve_specifier(file_rel, spec)
                if target:
                    t_file, t_name = follow_barrel(target, name, _depth + 1)
                    if t_name in defined_names(t_file):
                        return t_file, t_name
            return file_rel, name

        # Per file: local import name -> (target_rel, exported_name).
        # exported_name is "*" for namespace imports, None for default
        # imports (whose exported name we can't know without evaluating
        # the target's `export default`).
        import_maps: Dict[str, Dict[str, Tuple[str, Optional[str]]]] = {}
        for rel, f in facts.items():
            imap: Dict[str, Tuple[str, Optional[str]]] = {}
            for node in f.tree.root_node.named_children:
                if node.type != "import_statement":
                    continue
                spec = _import_source(node)
                if spec is None:
                    continue
                target = resolve_specifier(rel, spec)
                if target is None:
                    continue
                clause = next(
                    (c for c in node.named_children if c.type == "import_clause"),
                    None,
                )
                if clause is None:
                    continue
                for item in clause.named_children:
                    if item.type == "identifier":  # default import
                        imap[_text(item)] = (target, None)
                    elif item.type == "namespace_import":
                        ns_name = next(
                            (_text(c) for c in item.named_children
                             if c.type == "identifier"), None,
                        )
                        if ns_name:
                            imap[ns_name] = (target, "*")
                    elif item.type == "named_imports":
                        for imp_spec in item.named_children:
                            if imp_spec.type != "import_specifier":
                                continue
                            orig = imp_spec.child_by_field_name("name")
                            alias = imp_spec.child_by_field_name("alias")
                            if orig is None:
                                continue
                            local = _text(alias) if alias is not None else _text(orig)
                            imap[local] = follow_barrel(target, _text(orig))
            import_maps[rel] = imap

        # ── Symbol lookup helpers ────────────────────────────────────────

        def lookup_callable(target_file: str, name: Optional[str]) -> Optional[str]:
            """Symbol id for calling `name` defined in target_file:
            function/const binding, or Class -> Class.constructor."""
            if name is None or target_file not in facts:
                return None
            f = facts[target_file]
            for sym_name, node in f.symbols:
                if sym_name == name:
                    if node.type in _TYPE_DECLS:
                        return None  # types take no call edges
                    return f"{target_file}:{name}"
            if name in f.class_nodes and "constructor" in f.class_methods.get(name, ()):
                return f"{target_file}:{name}.constructor"
            return None

        def resolve_name(rel: str, name: str) -> Optional[str]:
            """A bare identifier used in `rel`: local def, else import."""
            local = lookup_callable(rel, name)
            if local:
                return local
            imported = import_maps[rel].get(name)
            if not imported:
                return None
            t_file, t_name = imported
            if t_name == "*":
                return None  # the namespace object itself, not a callable
            # Default imports (t_name None): best effort — try the local
            # binding name against the target file's definitions.
            return lookup_callable(t_file, t_name or name)

        def resolve_extends(rel: str, class_name: str) -> Optional[Tuple[str, str]]:
            """(file, ParentClass) for `class X extends Parent` when Parent
            is a class we indexed (locally or via import)."""
            node = facts[rel].class_nodes.get(class_name)
            parent = _extends_name(node)
            if parent is None:
                return None
            if parent in facts[rel].class_nodes:
                return rel, parent
            imported = import_maps[rel].get(parent)
            if imported and imported[1] != "*":
                t_file, t_name = imported
                t_name = t_name or parent
                if t_file in facts and t_name in facts[t_file].class_nodes:
                    return t_file, t_name
            return None

        def resolve_type(rel: str, type_name: str):
            """Where a type name used in `rel` is defined: ("class", file,
            name) for classes, ("type", file, symbol_id) for interfaces /
            type aliases / enums, None if not indexed."""
            def check(t_file: str, t_name: str):
                if t_file not in facts:
                    return None
                if t_name in facts[t_file].class_nodes:
                    return ("class", t_file, t_name)
                for sym_name, node in facts[t_file].symbols:
                    if sym_name == t_name and node.type in _TYPE_DECLS:
                        return ("type", t_file, f"{t_file}:{t_name}")
                return None
            found = check(rel, type_name)
            if found:
                return found
            imported = import_maps[rel].get(type_name)
            if imported and imported[1] != "*":
                t_file, t_name = imported
                return check(t_file, t_name or type_name)
            return None

        def method_edge_for_type(rel: str, type_name: str, method: str) -> Optional[str]:
            """Edge target for `receiver.method()` when receiver's declared
            type is `type_name`: the class method if it exists (following
            extends one level), else the interface/type symbol itself —
            the contract being invoked co-changes with its callers."""
            resolved = resolve_type(rel, type_name)
            if resolved is None:
                return None
            kind, t_file, t_name = resolved
            if kind == "type":
                return t_name          # symbol id of the interface/alias
            if method in facts[t_file].class_methods.get(t_name, ()):
                return f"{t_file}:{t_name}.{method}"
            parent = resolve_extends(t_file, t_name)
            if parent and method in facts[parent[0]].class_methods.get(parent[1], ()):
                return f"{parent[0]}:{parent[1]}.{method}"
            return None

        # ── Edges ────────────────────────────────────────────────────────
        graph: Dict[str, List[str]] = {}

        for rel, f in facts.items():
            def_node_ids = {id(node) for _n, node in f.symbols}

            for name, node in f.symbols:
                sym_id = f"{rel}:{name}"
                graph.setdefault(sym_id, [])
                if node.type in _TYPE_DECLS:
                    continue

                class_name = name.rsplit(".", 1)[0] if "." in name else None
                param_info = _param_info(node)
                param_names = set(param_info)
                local_types = _collect_local_types(_body_of(node), def_node_ids)
                edges = graph[sym_id]

                def type_of_receiver(receiver: str, _l=local_types, _p=param_info):
                    """Declared type of a local identifier: local vars
                    (annotation or `new X()`) shadow parameters."""
                    return _l.get(receiver) or _p.get(receiver)

                def add_edge(dep: Optional[str], _sym=sym_id, _edges=edges):
                    if dep and dep != _sym and dep not in _edges:
                        _edges.append(dep)

                for call in _iter_calls(_body_of(node), def_node_ids):
                    if call.type == "new_expression":
                        ctor = call.child_by_field_name("constructor")
                        if ctor is not None and ctor.type == "identifier":
                            cname = _text(ctor)
                            if cname not in param_names:
                                add_edge(resolve_name(rel, cname))
                        continue

                    fn = call.child_by_field_name("function")
                    if fn is None:
                        continue
                    if fn.type == "identifier":
                        callee = _text(fn)
                        if callee not in param_names:
                            add_edge(resolve_name(rel, callee))
                    elif fn.type == "super" and class_name:
                        parent = resolve_extends(rel, class_name)
                        if parent:
                            add_edge(lookup_callable(parent[0], parent[1]))
                    elif fn.type == "member_expression":
                        obj = fn.child_by_field_name("object")
                        prop = fn.child_by_field_name("property")
                        if obj is None or prop is None:
                            continue
                        method = _text(prop)
                        if obj.type == "this" and class_name:
                            if method in f.class_methods.get(class_name, ()):
                                add_edge(f"{rel}:{class_name}.{method}")
                        elif obj.type == "identifier":
                            imported = import_maps[rel].get(_text(obj))
                            if imported and imported[1] == "*":
                                add_edge(lookup_callable(imported[0], method))
                            else:
                                # Typed receiver: `u.login()` where u is a
                                # param/local declared `: User` or `new User()`
                                rtype = type_of_receiver(_text(obj))
                                if rtype:
                                    add_edge(method_edge_for_type(rel, rtype, method))
                        elif (
                            obj.type == "member_expression" and class_name
                        ):
                            # `this.field.method()` through a typed field or
                            # constructor parameter property
                            inner_obj = obj.child_by_field_name("object")
                            inner_prop = obj.child_by_field_name("property")
                            if (
                                inner_obj is not None
                                and inner_obj.type == "this"
                                and inner_prop is not None
                            ):
                                ftype = f.class_fields.get(class_name, {}).get(
                                    _text(inner_prop)
                                )
                                if ftype:
                                    add_edge(method_edge_for_type(rel, ftype, method))

                    # Function references passed as arguments (`arr.map(fn)`,
                    # `on('x', handler)`) are dependencies even though never
                    # called at this site — same rationale as the Python
                    # fn-ref edges.
                    args = call.child_by_field_name("arguments")
                    if args is not None:
                        for arg in args.named_children:
                            if arg.type != "identifier":
                                continue
                            ref = _text(arg)
                            if ref in param_names:
                                continue
                            target = resolve_name(rel, ref)
                            if target and not target.endswith(".constructor"):
                                add_edge(target)

                # Annotation-reference edges: consumer → interface/alias it
                # mentions in its signature (Python's annotated-return-edge
                # analog). Only TYPE declarations — classes get edges from
                # real calls. Direction is consumer → type, so a changed
                # interface pulls all its consumers into the blast radius
                # via the reverse graph, which is exactly what a types/*.ts
                # edit's co-change history shows.
                ann_types = _symbol_annotation_types(node)
                ann_types.update(v for v in param_info.values() if v)
                ann_types.update(local_types.values())
                for tname in ann_types:
                    resolved_t = resolve_type(rel, tname)
                    if resolved_t is not None and resolved_t[0] == "type":
                        add_edge(resolved_t[2])

            # Child → parent override edges via extends (mirrors the Python
            # graph's Phase 1A; same direction rationale — never parent →
            # all children, which would create mega-hubs).
            for cls, methods in f.class_methods.items():
                parent = resolve_extends(rel, cls)
                if not parent:
                    continue
                p_file, p_cls = parent
                parent_methods = facts[p_file].class_methods.get(p_cls, set())
                for m in methods:
                    if m in parent_methods:
                        child_id = f"{rel}:{cls}.{m}"
                        parent_id = f"{p_file}:{p_cls}.{m}"
                        graph.setdefault(child_id, [])
                        if parent_id not in graph[child_id]:
                            graph[child_id].append(parent_id)

        return graph


# TSConfig is JSONC: comments and trailing commas are legal. This regex
# pass is approximate (a `//` inside a string would be eaten) but tsconfig
# path values are file globs where that can't occur in practice.
_JSONC_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_JSONC_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def _load_tsconfig(cfg_path: str):
    """(config_dir, baseUrl, paths) from a tsconfig/jsconfig file, or None
    when unreadable. `extends` chains are not followed (v1, disclosed)."""
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
        text = _JSONC_COMMENT.sub("", text)
        text = _JSONC_TRAILING_COMMA.sub(r"\1", text)
        data = json.loads(text)
    except (OSError, ValueError):
        return None
    opts = data.get("compilerOptions", {}) if isinstance(data, dict) else {}
    base_url = opts.get("baseUrl", "") or ""
    raw_paths = opts.get("paths", {}) or {}
    paths = {
        k: v for k, v in raw_paths.items()
        if isinstance(k, str) and isinstance(v, list)
    }
    if not base_url and not paths:
        return None
    return os.path.dirname(os.path.abspath(cfg_path)), base_url, paths


# ── Tree walking (module-level; stateless) ───────────────────────────────

def _text(node) -> str:
    return node.text.decode("utf-8", "ignore")


def _import_source(node) -> Optional[str]:
    src = node.child_by_field_name("source")
    if src is None:
        return None
    frag = next(
        (c for c in src.named_children if c.type == "string_fragment"), None
    )
    return _text(frag) if frag is not None else None


def _gather_facts(tree) -> _FileFacts:
    """One walk: symbols, class nodes/methods, and barrel re-exports."""
    facts = _FileFacts(tree)
    stack: List[str] = []

    def qualify(name: str) -> str:
        return ".".join(stack + [name]) if stack else name

    def walk(node):
        for child in node.named_children:
            if child.type == "export_statement":
                decl = child.child_by_field_name("declaration")
                if decl is not None:
                    handle(decl)
            else:
                handle(child)

    def handle(node):
        t = node.type
        if t in _FUNCTION_DECLS:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                facts.symbols.append((qualify(_text(name_node)), node))
                body = node.child_by_field_name("body")
                if body is not None:
                    walk(body)
        elif t in _CLASS_DECLS:
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is not None and body is not None:
                cls_qualified = qualify(_text(name_node))
                facts.class_nodes[cls_qualified] = node
                methods = facts.class_methods.setdefault(cls_qualified, set())
                fields = facts.class_fields.setdefault(cls_qualified, {})
                stack.append(_text(name_node))
                for member in body.named_children:
                    if member.type == "method_definition":
                        m_name = member.child_by_field_name("name")
                        if m_name is not None:
                            facts.symbols.append((qualify(_text(m_name)), member))
                            methods.add(_text(m_name))
                            if _text(m_name) == "constructor":
                                # Parameter properties: `constructor(private
                                # db: Database)` declares field `db`.
                                for pname, ptype in _parameter_properties(member):
                                    if ptype:
                                        fields[pname] = ptype
                            m_body = member.child_by_field_name("body")
                            if m_body is not None:
                                walk(m_body)
                    elif member.type == "public_field_definition":
                        f_name = member.child_by_field_name("name")
                        f_type = _annotation_type(member.child_by_field_name("type"))
                        if f_name is not None and f_type:
                            fields[_text(f_name)] = f_type
                stack.pop()
        elif t in _TYPE_DECLS:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                facts.symbols.append((qualify(_text(name_node)), node))
        elif t in ("lexical_declaration", "variable_declaration"):
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if (
                    name_node is not None
                    and name_node.type == "identifier"
                    and value is not None
                    and value.type in _FUNCTION_VALUES
                ):
                    facts.symbols.append((qualify(_text(name_node)), declarator))
                    body = value.child_by_field_name("body")
                    if body is not None and body.type == "statement_block":
                        walk(body)
        elif t == "internal_module":  # namespace X { ... }
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is not None and body is not None:
                stack.append(_text(name_node))
                walk(body)
                stack.pop()
        elif t == "expression_statement":
            for child in node.named_children:
                handle(child)
        elif t in _DESCEND_STMTS:
            walk(node)

    walk(tree.root_node)
    facts.reexports = _collect_reexports(tree.root_node)
    return facts


def _collect_reexports(root) -> Tuple[Dict[str, Tuple[str, str]], List[str]]:
    """
    Barrel-file exports: ({exported_name: (specifier, original_name)},
    [star_specifiers]) from `export {X as Y} from './x'` / `export * from`.
    """
    named: Dict[str, Tuple[str, str]] = {}
    stars: List[str] = []
    for node in root.named_children:
        if node.type != "export_statement":
            continue
        spec = _import_source(node)
        if spec is None:
            continue
        clause = next(
            (c for c in node.named_children if c.type == "export_clause"), None
        )
        if clause is None:
            stars.append(spec)  # export * from './x'
            continue
        for exp in clause.named_children:
            if exp.type != "export_specifier":
                continue
            orig = exp.child_by_field_name("name")
            alias = exp.child_by_field_name("alias")
            if orig is None:
                continue
            exported = _text(alias) if alias is not None else _text(orig)
            named[exported] = (spec, _text(orig))
    return named, stars


def _body_of(node):
    """The block whose calls belong to this symbol. For arrow/function
    consts the body lives on the declarator's value, not the node."""
    if node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        return value.child_by_field_name("body") if value is not None else None
    return node.child_by_field_name("body")


def _annotation_type(type_annotation) -> Optional[str]:
    """Class-ish type name from a type_annotation node: `: User` -> "User",
    `: Repo<User>` -> "Repo", `: ns.User` -> "User". Predefined types
    (string, number...) and complex types return None."""
    if type_annotation is None:
        return None
    for child in type_annotation.named_children:
        if child.type == "type_identifier":
            return _text(child)
        if child.type == "generic_type":
            name = child.child_by_field_name("name")
            if name is not None and name.type == "type_identifier":
                return _text(name)
        if child.type == "nested_type_identifier":
            return _text(child).rsplit(".", 1)[-1]
    return None


def _params_of(node):
    """The formal_parameters node of a function-ish definition."""
    params = node.child_by_field_name("parameters")
    if params is None and node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        if value is not None:
            params = value.child_by_field_name("parameters")
    return params


def _param_info(node) -> "Dict[str, Optional[str]]":
    """Parameter name -> declared type name (or None) of a definition
    node. Names double as the shadow guard; types feed receiver
    resolution (`u: User` ... `u.login()` -> User.login)."""
    info: "Dict[str, Optional[str]]" = {}
    params = _params_of(node)
    if params is None:
        return info
    for p in params.named_children:
        if p.type == "identifier":
            info[_text(p)] = None
        elif p.type in ("required_parameter", "optional_parameter"):
            pattern = p.child_by_field_name("pattern")
            if pattern is not None and pattern.type == "identifier":
                info[_text(pattern)] = _annotation_type(
                    p.child_by_field_name("type")
                )
    return info


def _type_names_under(node, out: Set[str]) -> None:
    """Every type_identifier under `node`, generic arguments included
    (`Partial<KyOptions>` yields both Partial and KyOptions)."""
    if node.type == "type_identifier":
        out.add(_text(node))
    elif node.type == "nested_type_identifier":
        out.add(_text(node).rsplit(".", 1)[-1])
        return
    for child in node.named_children:
        _type_names_under(child, out)


def _symbol_annotation_types(node) -> Set[str]:
    """
    Type names this definition MENTIONS in its signature — parameter and
    return annotations. In TypeScript, implementations co-change with the
    interfaces/aliases they annotate with (a `types/*.ts` edit lands in
    the same commit as its consumers), a dependency class that call
    scanning can never see because types are never called.
    """
    names: Set[str] = set()
    params = _params_of(node)
    if params is not None:
        _type_names_under(params, names)
    ret = node.child_by_field_name("return_type")
    if ret is None and node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        if value is not None:
            ret = value.child_by_field_name("return_type")
    if ret is not None:
        _type_names_under(ret, names)
    return names


def _parameter_properties(ctor_node):
    """(name, type) for constructor params with an accessibility modifier
    (`constructor(private db: Database)`) — TS declares them as fields."""
    params = _params_of(ctor_node)
    if params is None:
        return
    for p in params.named_children:
        if p.type not in ("required_parameter", "optional_parameter"):
            continue
        if not any(c.type == "accessibility_modifier" for c in p.children):
            continue
        pattern = p.child_by_field_name("pattern")
        if pattern is not None and pattern.type == "identifier":
            yield _text(pattern), _annotation_type(p.child_by_field_name("type"))


def _collect_local_types(body, def_node_ids: Set[int]) -> Dict[str, str]:
    """
    Local variable name -> type name within a symbol's body, from explicit
    annotations (`const u: User = ...`) and constructor inference
    (`const u = new User()`). Walk boundaries match _iter_calls, so the
    env covers exactly the calls it will be used to resolve.
    """
    env: Dict[str, str] = {}
    if body is None:
        return env
    stack = [body]
    while stack:
        node = stack.pop()
        if id(node) in def_node_ids:
            continue
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            if name_node is not None and name_node.type == "identifier":
                declared = _annotation_type(node.child_by_field_name("type"))
                if declared is None:
                    value = node.child_by_field_name("value")
                    if value is not None and value.type == "new_expression":
                        ctor = value.child_by_field_name("constructor")
                        if ctor is not None and ctor.type == "identifier":
                            declared = _text(ctor)
                if declared:
                    env[_text(name_node)] = declared
        stack.extend(node.named_children)
    return env


def _extends_name(node) -> Optional[str]:
    """Parent class identifier from `class X extends Parent`, else None."""
    if node is None or node.type not in _CLASS_DECLS:
        return None
    for child in node.named_children:
        if child.type == "class_heritage":
            for clause in child.named_children:
                if clause.type == "extends_clause":
                    value = clause.child_by_field_name("value")
                    if value is not None and value.type == "identifier":
                        return _text(value)
    return None


def _iter_calls(body, def_node_ids: Set[int]):
    """
    Yield call_expression / new_expression nodes inside `body`, without
    descending into nested nodes that are themselves collected symbols
    (their calls belong to them) — but descending into anonymous inline
    callbacks, whose calls belong to the enclosing symbol.
    """
    if body is None:
        return
    # Seed with the body node itself, not its children: an expression-bodied
    # arrow (`=> new Foo()`) has the call AS the body.
    stack = [body]
    while stack:
        node = stack.pop()
        if id(node) in def_node_ids:
            continue
        if node.type in ("call_expression", "new_expression"):
            yield node
        stack.extend(node.named_children)
