"""
symbols.py — Attribute ownership extraction for resolving self.attr.method() chains.

Given a class with `self.router = APIRouter()`, this module figures out that
`self.router` has type `APIRouter`, enabling cross-file method resolution.
"""

import ast
from typing import Dict, Optional, Tuple


def _iter_statements(body):
    """Yield statements in source order, recursing into if/for/while/with/try."""
    for stmt in body:
        yield stmt

        if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            yield from _iter_statements(stmt.body)
            yield from _iter_statements(stmt.orelse)

        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            yield from _iter_statements(stmt.body)

        elif isinstance(stmt, ast.Try):
            yield from _iter_statements(stmt.body)
            for handler in stmt.handlers:
                yield from _iter_statements(handler.body)
            yield from _iter_statements(stmt.orelse)
            yield from _iter_statements(stmt.finalbody)


def _extract_annotation_name(annotation) -> Optional[Tuple[Optional[str], str]]:
    """
    Returns (qualifier, bare_name) or None.

    Router          ->  (None, "Router")
    Optional[Router]->  (None, "Router")       <- unwrap subscript
    routing.Router  ->  ("routing", "Router")
    """
    if isinstance(annotation, ast.Name):
        return (None, annotation.id)

    if isinstance(annotation, ast.Attribute) and isinstance(annotation.value, ast.Name):
        return (annotation.value.id, annotation.attr)

    # Optional[Router], List[Router], etc.  — unwrap one level
    if isinstance(annotation, ast.Subscript):
        return _extract_annotation_name(annotation.slice)

    return None


def _extract_call_owner(value) -> Optional[Tuple[Optional[str], str]]:
    """
    Returns (qualifier, bare_name) or None.

    APIRouter()          ->  (None,      "APIRouter")
    routing.APIRouter()  ->  ("routing", "APIRouter")
    make_helper()        ->  (None,      "make_helper")
    """
    if not isinstance(value, ast.Call):
        return None

    func = value.func

    if isinstance(func, ast.Name):
        return (None, func.id)

    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr)

    if isinstance(func, ast.Attribute):
        return (None, func.attr)

    return None


def _extract_assign_owner(value, local_var_types=None, param_types=None):
    """
    Handles non-Call RHS cases that still indicate a type:
      self.x = router or Router()   -> BoolOp -> try each operand
      self.x = router               -> bare Name -> no type info (return None)
    """
    if local_var_types is None:
        local_var_types = {}
    if param_types is None:
        param_types = {}

    if isinstance(value, ast.Call):
        return _extract_call_owner(value)

    if isinstance(value, ast.Name):
        if value.id in param_types:
            return param_types[value.id]
        if value.id in local_var_types:
            return local_var_types[value.id]
        return None

    if isinstance(value, ast.BoolOp):
        call_result = None
        name_result = None
        for operand in value.values:
            ref = _extract_assign_owner(operand, local_var_types, param_types)
            if ref is None:
                continue
            if isinstance(operand, ast.Call):
                call_result = call_result or ref
            else:
                name_result = name_result or ref
        return call_result or name_result

    return None


def extract_local_var_types(
    fn_node,
    param_types: Optional[Dict[str, Tuple[Optional[str], str]]] = None,
) -> Dict[str, Tuple[Optional[str], str]]:
    """
    Track local variable -> type assignments within a SINGLE function body,
    free function or method alike:

        h = Helper()              -> {"h": (None, "Helper")}
        r: routing.Router = ...   -> {"r": ("routing", "Router")}
        x = make_helper()         -> chases factory functions the same way
                                      attribute ownership tracking does
                                      (resolution of the factory return type
                                      itself happens later, in graph_builder)

    This is the free-function counterpart to the self.attr tracking that
    extract_attribute_ownerships does for class bodies. It exists because
    a huge fraction of real code instantiates a class in a local variable
    inside a plain function (not as a self.attr) and immediately calls a
    method on it -- e.g.:

        def run():
            h = Handler()
            return h.process()

    Without this, `h.process()` can never resolve: there's nowhere that
    records "h" has type "Handler". extract_attribute_ownerships alone
    can't help, because it only ever looks inside ast.ClassDef bodies.

    Returns: {local_var_name: (qualifier, bare_type_name)}
    """
    if param_types is None:
        param_types = {}

    local_var_types: Dict[str, Tuple[Optional[str], str]] = {}

    for stmt in _iter_statements(fn_node.body):

        if isinstance(stmt, ast.AnnAssign):
            target = stmt.target
            if isinstance(target, ast.Name):
                ref = _extract_annotation_name(stmt.annotation)
                if ref:
                    local_var_types[target.id] = ref
            continue

        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    ref = _extract_assign_owner(
                        stmt.value, local_var_types, param_types
                    )
                    if ref:
                        local_var_types[target.id] = ref
            continue

    return local_var_types


def extract_param_types(fn_node) -> Dict[str, Tuple[Optional[str], str]]:
    """
    Map annotated parameter names to their (qualifier, bare_name) type, e.g.

        def f(router: routing.Router, name: str): ...

    -> {"router": ("routing", "Router")}   (unannotated/non-type params skipped)
    """
    param_types: Dict[str, Tuple[Optional[str], str]] = {}
    for arg in fn_node.args.args:
        if arg.annotation is not None:
            ref = _extract_annotation_name(arg.annotation)
            if ref:
                param_types[arg.arg] = ref
    return param_types


def extract_attribute_ownerships(tree) -> Dict[str, Tuple[Optional[str], str]]:
    """
    Extracts mappings like:
        self.router: routing.Router = routing.Router()
        self.router = Router()
        self.router2 = router            # typed constructor param
        h = Helper(); self.helper = h    # local var tracking
        self.router3 = router or Router()
        self.opt_router: Optional[Router] = None

    Returns: {"FastAPI.router": (qualifier, type_name), ...}
    """
    ownerships: Dict[str, Tuple[Optional[str], str]] = {}

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue

        class_name = node.name

        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            param_types = extract_param_types(item)

            local_var_types = {}

            for stmt in _iter_statements(item.body):

                if isinstance(stmt, ast.AnnAssign):
                    target = stmt.target
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        ref = _extract_annotation_name(stmt.annotation)
                        if ref:
                            ownerships[f"{class_name}.{target.attr}"] = ref
                    continue

                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            ref = _extract_assign_owner(
                                stmt.value, local_var_types, param_types
                            )
                            if ref:
                                ownerships[f"{class_name}.{target.attr}"] = ref

                        elif isinstance(target, ast.Name):
                            ref = _extract_assign_owner(
                                stmt.value, local_var_types, param_types
                            )
                            if ref:
                                local_var_types[target.id] = ref
                    continue

    return ownerships