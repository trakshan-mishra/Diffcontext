"""
PATCH for attribute_ownership.py
---------------------------------
BUG: _extract_call_owner returns bare string (loses module qualifier).
     self.router = routing.APIRouter()  →  stored as "APIRouter" (wrong)
     _resolve_owner_type then can't find "APIRouter" in import_map because
     the file imports 'routing' (module), not 'APIRouter' (class) directly.

FIX: Return (qualifier, bare_name) tuples consistently, same as
     _extract_annotation_name should also do.

Drop-in replacements for the two helper functions.
"""

import ast


def _extract_annotation_name(annotation):
    """
    Returns (qualifier, bare_name) or None.

    Router          →  (None, "Router")
    Optional[Router]→  (None, "Router")       ← unwrap subscript
    routing.Router  →  ("routing", "Router")
    """
    if isinstance(annotation, ast.Name):
        return (None, annotation.id)

    if isinstance(annotation, ast.Attribute) and isinstance(annotation.value, ast.Name):
        return (annotation.value.id, annotation.attr)

    # Optional[Router], List[Router], etc.  — unwrap one level
    if isinstance(annotation, ast.Subscript):
        return _extract_annotation_name(annotation.slice)

    return None


def _extract_call_owner(value):
    """
    Returns (qualifier, bare_name) or None.

    APIRouter()          →  (None,      "APIRouter")
    routing.APIRouter()  →  ("routing", "APIRouter")   ← FIX: was returning "APIRouter"
    make_helper()        →  (None,      "make_helper")
    """
    if not isinstance(value, ast.Call):
        # Also handle bare Name / BoolOp (router or Router()) – caller handles
        return None

    func = value.func

    if isinstance(func, ast.Name):
        return (None, func.id)

    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr)

    if isinstance(func, ast.Attribute):
        # deep chain like a.b.C() — best-effort: use innermost name
        return (None, func.attr)

    return None


def _extract_assign_owner(value):
    """
    Handles non-Call RHS cases that still indicate a type:
      self.x = router or Router()   → BoolOp → try each operand
      self.x = router               → bare Name → no type info (return None)
    """
    if isinstance(value, ast.BoolOp):
        for operand in value.values:
            result = _extract_call_owner(operand)
            if result:
                return result
    return _extract_call_owner(value)
