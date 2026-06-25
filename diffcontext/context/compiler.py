"""
compiler.py — Compile selected symbols into an LLM-ready context package.

Key additions over the naive "dump code blocks" approach:
  - META header: LLM knows repo size, what was dropped, graph confidence,
    broken files, and scoring basis BEFORE reading any code.
  - Per-symbol relationship annotations: callers / callees, and explicit
    "NOT IN CONTEXT" tags so the LLM doesn't hallucinate missing deps.
  - Dropped-symbol manifest: explicit list of symbols cut by token budget.
  - Graph confidence score: fraction of edges that resolved to known symbols.
  - Auto-generated suggestions: rule-based hints derived from graph data.
"""

import ast
import re
from typing import Dict, List, Optional, Set, Tuple

from ..models import Symbol, ContextPackage


def _get_module_docstring(abs_file_path: str) -> str:
    """
    Read the first line of the module-level docstring from an ABSOLUTE path.
    Returns "" if file is unreadable or has no docstring.
    Must receive sym.file (absolute), NOT the relative path from sym_id.
    """
    try:
        with open(abs_file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        doc = ast.get_docstring(tree)
        if doc:
            first_line = doc.strip().split("\n")[0]
            first_line = re.sub(r'^[\w/\-\.]+\.py\s*[—\-]+\s*', '', first_line).strip()
            if len(first_line) > 60:
                return first_line[:57] + "..."
            return first_line
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compile_context(
    symbols: Dict[str, Symbol],
    selected_ids: List[str],
    changed_ids: List[str],
    scores: Dict[str, float],
    graph: Optional[Dict[str, List[str]]] = None,
    dropped_ids: Optional[List[str]] = None,
    skipped_files: Optional[List[str]] = None,
    notes: Optional[str] = None,
) -> ContextPackage:
    """
    Build the final context text from selected symbols.

    Args:
        symbols:       Full symbol table for the repo.
        selected_ids:  Symbols chosen for this context (respects token budget).
        changed_ids:   The symbols that actually changed (always selected).
        scores:        Impact scores for all scored symbols.
        graph:         Full call graph (id -> [dep ids]). Enables relationship
                       annotations and confidence calculation.
        dropped_ids:   Symbols that were scored but cut by token budget.
        skipped_files: Files that raised SyntaxError (graph has holes here).
        notes:         Optional user notes injected into meta header.
    """
    dropped_ids   = dropped_ids   or []
    skipped_files = skipped_files or []

    changed_set  = set(changed_ids)
    selected_set = set(selected_ids)

    # Build reverse graph for caller annotation
    reverse: Dict[str, Set[str]] = {}
    if graph:
        for caller, callees in graph.items():
            for callee in callees:
                reverse.setdefault(callee, set()).add(caller)

    # Graph confidence: fraction of edges that point to a known symbol
    graph_confidence = _compute_confidence(graph, symbols)

    # Token bookkeeping
    total_repo_code   = "\n\n".join(s.code for s in symbols.values())
    total_repo_tokens = max(1, len(total_repo_code) // 4)

    # --- Bucket symbols into sections ---
    sections: Dict[str, List[str]] = {"CHANGED": [], "IMPACTED": [], "DEPENDENCIES": []}

    for sym_id in selected_ids:
        if sym_id not in symbols:
            continue

        sym   = symbols[sym_id]
        score = scores.get(sym_id, 0)
        file_name, func_name = sym_id.split(":", 1)

        # Relationship annotation block
        rel_block = _build_relationship_block(
            sym_id, graph, reverse, selected_set, symbols
        )

        entry = (
            f"FILE: {file_name}\n"
            f"FUNCTION: {func_name} (score: {score:.0f})\n"
            + rel_block +
            f"\n{sym.code}"
        )

        if sym_id in changed_set:
            sections["CHANGED"].append(entry)
        elif score >= 70:
            sections["IMPACTED"].append(entry)
        else:
            sections["DEPENDENCIES"].append(entry)

    # --- Assemble code sections ---
    parts = []
    for label, entries in sections.items():
        if entries:
            parts.append(f"=== {label} SYMBOLS ===\n")
            parts.append("\n\n---\n\n".join(entries))

    code_text = "\n\n".join(parts)
    context_tokens = max(1, len(code_text) // 4)

    # --- Build meta-header (prepended so LLM reads it first) ---
    meta = _build_meta_header(
        symbols        = symbols,
        selected_ids   = selected_ids,
        dropped_ids    = dropped_ids,
        skipped_files  = skipped_files,
        changed_ids    = changed_ids,
        graph          = graph,
        reverse        = reverse,
        graph_confidence = graph_confidence,
        token_budget   = total_repo_tokens,   # not the budget cap; just total repo
        context_tokens = context_tokens,
        scores         = scores,
        notes          = notes,
    )

    # --- Build suggestions block (appended at bottom) ---
    suggestions = _build_suggestions(
        changed_ids      = changed_ids,
        dropped_ids      = dropped_ids,
        skipped_files    = skipped_files,
        graph            = graph,
        reverse          = reverse,
        graph_confidence = graph_confidence,
        scores           = scores,
    )

    full_text = meta + "\n\n" + code_text
    if suggestions:
        full_text += "\n\n" + suggestions

    return ContextPackage(
        text               = full_text,
        symbol_count       = len(selected_ids),
        token_estimate     = context_tokens,
        total_repo_tokens  = total_repo_tokens,
        dropped_symbols    = dropped_ids,
        skipped_files      = skipped_files,
        graph_confidence   = graph_confidence,
    )


# ---------------------------------------------------------------------------
# Meta-header
# ---------------------------------------------------------------------------

def _build_meta_header(
    symbols: Dict[str, Symbol],
    selected_ids: List[str],
    dropped_ids: List[str],
    skipped_files: List[str],
    changed_ids: List[str],
    graph: Optional[Dict[str, List[str]]],
    reverse: Dict[str, Set[str]],
    graph_confidence: float,
    token_budget: int,
    context_tokens: int,
    scores: Dict[str, float],
    notes: Optional[str] = None,
) -> str:
    total_syms    = len(symbols)
    selected_cnt  = len(selected_ids)
    dropped_cnt   = len(dropped_ids)
    scored_cnt    = len(scores)
    total_edges   = sum(len(v) for v in graph.values()) if graph else 0

    direct_callers  = sum(
        1 for s in changed_ids
        for c in reverse.get(s, set())
    )
    direct_callees  = sum(
        len(graph.get(s, []))
        for s in changed_ids
    ) if graph else 0

    lines = [
        "=== DIFFCONTEXT META ===",
        f"Repo symbols total    : {total_syms}",
        f"Symbols scored        : {scored_cnt}",
        f"Symbols IN context    : {selected_cnt}",
        f"Symbols DROPPED       : {dropped_cnt}  ← you cannot see these",
        f"Graph edges total     : {total_edges}",
        f"Graph confidence      : {graph_confidence * 100:.0f}%"
          + ("  ✓" if graph_confidence >= 0.9 else "  ⚠ incomplete"),
        f"Changed symbols       : {len(changed_ids)}",
        f"Direct callers found  : {direct_callers}",
        f"Direct callees found  : {direct_callees}",
        f"Context tokens (est.) : {context_tokens:,}",
        f"Scoring basis         : changed=100 | direct_callee=90 | direct_caller=80 "
          f"| 2hop_callee=60 | 2hop_caller=50 | indegree*2+outdegree bonus",
    ]

    # --- Repository Architecture Snapshot ---
    # Build rel_file -> absolute_path mapping from symbol table.
    # sym_id gives us relative path; sym.file gives us the absolute path we
    # need to actually open the file for its docstring.
    rel_to_abs: Dict[str, str] = {}
    for sym_id, sym in symbols.items():
        rel_file = sym_id.split(":", 1)[0]
        if rel_file not in rel_to_abs:
            rel_to_abs[rel_file] = sym.file  # sym.file is always absolute

    modules_total = {}
    modules_selected = {}
    for sym_id in symbols:
        file_name = sym_id.split(":", 1)[0]
        modules_total[file_name] = modules_total.get(file_name, 0) + 1
        
    for sym_id in selected_ids:
        if sym_id in symbols:
            file_name = sym_id.split(":", 1)[0]
            modules_selected[file_name] = modules_selected.get(file_name, 0) + 1

    lines.append("")
    lines.append("=== REPOSITORY ARCHITECTURE SNAPSHOT ===")
    
    loaded_files = []
    blind_files = []
    
    for file_name, total in sorted(modules_total.items()):
        selected = modules_selected.get(file_name, 0)
        
        doc_snippet = ""
        if file_name.endswith(".py"):
            # FIX: use absolute path, not the relative file_name
            abs_path = rel_to_abs.get(file_name, "")
            doc_str = _get_module_docstring(abs_path) if abs_path else ""
            if doc_str:
                doc_snippet = f" — {doc_str}"
                
        if selected > 0:
            loaded_files.append(f"  - {file_name} ({selected}/{total} symbols loaded){doc_snippet}")
        else:
            blind_files.append(f"  - {file_name} ({total} symbols){doc_snippet}")

    lines.append("MODULES IN CONTEXT:")
    if loaded_files:
        lines.extend(loaded_files)
    else:
        lines.append("  (none)")
        
    lines.append("")
    lines.append("KNOWN MODULES (NOT IN CONTEXT - BLIND SPOTS):")
    if blind_files:
        lines.extend(blind_files)
    else:
        lines.append("  (none)")

    if skipped_files:
        lines.append("")
        lines.append(f"FILES WITH SYNTAXERROR ({len(skipped_files)}) — graph has holes here:")
        for f in skipped_files:
            lines.append(f"  ✗ {f}")

    if dropped_cnt > 0:
        lines.append("")
        lines.append(f"DROPPED SYMBOLS ({dropped_cnt}) — scored but cut by token budget:")
        for d in dropped_ids[:15]:
            lines.append(f"  - {d}  (score: {scores.get(d, 0):.0f})")
        if dropped_cnt > 15:
            lines.append(f"  ... and {dropped_cnt - 15} more")
        lines.append("  → If any of these are critical, re-run with a higher --max-tokens.")

    warnings = []
    if skipped_files:
        warnings.append(
            f"⚠ {len(skipped_files)} file(s) had SyntaxErrors. "
            "Call graph may be incomplete for those files."
        )
    if dropped_cnt > 0:
        warnings.append(
            f"⚠ {dropped_cnt} symbol(s) were dropped. "
            "References to them in the code below are NOT backed by visible implementations."
        )
    if graph_confidence < 0.8:
        warnings.append(
            f"⚠ Graph confidence is {graph_confidence * 100:.0f}%. "
            "Many calls could not be resolved — likely external/stdlib deps or dynamic dispatch."
        )

    if warnings:
        lines.append("")
        for w in warnings:
            lines.append(w)

    if notes:
        lines.append(f"\n=== DEVELOPER NOTES ===\n{notes}")

    lines.append("=== END META ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-symbol relationship block
# ---------------------------------------------------------------------------

def _build_relationship_block(
    sym_id: str,
    graph: Optional[Dict[str, List[str]]],
    reverse: Dict[str, Set[str]],
    selected_set: Set[str],
    symbols: Dict[str, Symbol],
) -> str:
    if not graph:
        return ""

    lines = []

    callers = sorted(reverse.get(sym_id, set()))
    callees = graph.get(sym_id, [])

    if callers:
        caller_parts = []
        for c in callers[:6]:
            tag = "" if c in selected_set else " [NOT IN CONTEXT]"
            caller_parts.append(c + tag)
        if len(callers) > 6:
            caller_parts.append(f"... +{len(callers) - 6} more")
        lines.append(f"CALLERS: {', '.join(caller_parts)}")

    if callees:
        callee_parts = []
        for c in callees[:6]:
            tag = "" if c in selected_set else " [NOT IN CONTEXT]"
            callee_parts.append(c + tag)
        if len(callees) > 6:
            callee_parts.append(f"... +{len(callees) - 6} more")
        lines.append(f"CALLEES: {', '.join(callee_parts)}")

    if not callers and not callees:
        lines.append("CALLERS: (none found in repo)")
        lines.append("CALLEES: (none found in repo)")

    return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------------------
# Suggestions block
# ---------------------------------------------------------------------------

def _build_suggestions(
    changed_ids, dropped_ids, skipped_files,
    graph, reverse, graph_confidence, scores,
) -> str:
    tips = []

    if skipped_files:
        tips.append(
            f"Fix SyntaxErrors in {len(skipped_files)} file(s) to improve graph accuracy."
        )

    if graph_confidence < 0.7:
        tips.append(
            f"Graph confidence is {graph_confidence * 100:.0f}%. "
            "Consider running diffcontext on installed site-packages too, "
            "or add the dependency source to your repo path."
        )

    for sym_id in changed_ids:
        callers = list(reverse.get(sym_id, set()))
        if len(callers) > 20:
            tips.append(
                f"'{sym_id.split(':')[-1]}' has {len(callers)} callers — "
                "unusually high blast radius. Review changes carefully."
            )

    if dropped_ids:
        tips.append(
            f"{len(dropped_ids)} symbol(s) were dropped by token budget. "
            "Run with --max-tokens=0 (unlimited) to see full context."
        )

    if not tips:
        return ""

    lines = ["=== DIFFCONTEXT SUGGESTIONS ==="]
    for i, tip in enumerate(tips, 1):
        lines.append(f"  {i}. {tip}")
    lines.append("=== END SUGGESTIONS ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Graph confidence
# ---------------------------------------------------------------------------

def _compute_confidence(
    graph: Optional[Dict[str, List[str]]],
    symbols: Dict[str, Symbol],
) -> float:
    """
    Fraction of graph edges that resolve to a known symbol.
    Edges to external/stdlib deps count as unresolved.
    Returns 1.0 if graph is empty or None (no data = no known holes).
    """
    if not graph:
        return 1.0

    total    = 0
    resolved = 0

    for deps in graph.values():
        for d in deps:
            total += 1
            if d in symbols:
                resolved += 1

    return resolved / total if total > 0 else 1.0