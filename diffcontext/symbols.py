"""
symbols.py — Attribute ownership extraction for resolving self.attr.method() chains.

Given a class with `self.router = APIRouter()`, this module figures out that
`self.router` has type `APIRouter`, enabling cross-file method resolution.
"""

import ast
from typing import Dict, Optional, Tuple, List


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

            param_types = {}
            for arg in item.args.args:
                if arg.annotation is not None:
                    ref = _extract_annotation_name(arg.annotation)
                    if ref:
                        param_types[arg.arg] = ref

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
