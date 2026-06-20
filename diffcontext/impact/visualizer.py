"""
visualizer.py — Visual blast radius renderer.

Renders the blast radius as a colored, indented tree showing:
  - The changed symbol at the root
  - Direct callers (who calls this?)
  - Direct callees (what does this call?)
  - Transitive impact propagation
  - Proof chains: the actual code line creating each edge

Designed for terminal output with ANSI colors.
"""

import ast
import os
import re
from typing import Dict, List, Optional, Set, Tuple


# ANSI color codes
class _C:
    RED = "\033[91m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BLUE = "\033[94m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    WHITE = "\033[97m"


def render_blast_radius(
    graph: Dict[str, List[str]],
    changed_symbols: List[str],
    symbols: dict,
    max_depth: int = 3,
    show_proof: bool = False,
    repo_path: str = "",
) -> str:
    """
    Render a visual tree of the blast radius for changed symbols.

    Returns a formatted string ready for terminal output.
    """
    # Build reverse graph (callers)
    reverse: Dict[str, Set[str]] = {}
    for caller, callees in graph.items():
        for callee in callees:
            reverse.setdefault(callee, set()).add(caller)

    lines: List[str] = []

    lines.append("")
    lines.append(f"{_C.BOLD}{_C.WHITE}{'═' * 70}{_C.RESET}")
    lines.append(f"{_C.BOLD}{_C.WHITE}  BLAST RADIUS ANALYSIS{_C.RESET}")
    lines.append(f"{_C.BOLD}{_C.WHITE}{'═' * 70}{_C.RESET}")

    for sym_id in changed_symbols:
        lines.append("")
        lines.append(f"  {_C.BOLD}{_C.RED}⚡ CHANGED:{_C.RESET} {_C.BOLD}{sym_id}{_C.RESET}")

        # Show symbol location info
        if sym_id in symbols:
            sym = symbols[sym_id]
            lines.append(f"  {_C.DIM}   File: {sym.file}{_C.RESET}")
            lines.append(f"  {_C.DIM}   Line: {sym.lineno}{_C.RESET}")

        lines.append("")

        # ---- CALLERS (who is affected by this change?) ----
        direct_callers = sorted(reverse.get(sym_id, set()))
        lines.append(f"  {_C.BOLD}{_C.YELLOW}▲ WHO CALLS THIS? (directly affected){_C.RESET}")

        if not direct_callers:
            lines.append(f"  {_C.DIM}  (no direct callers found){_C.RESET}")
        else:
            for i, caller in enumerate(direct_callers):
                is_last = i == len(direct_callers) - 1
                prefix = "└──" if is_last else "├──"
                lines.append(f"  {_C.YELLOW}  {prefix} {caller}{_C.RESET}")

                if show_proof:
                    proof = _find_proof(caller, sym_id, symbols, graph)
                    if proof:
                        pad = "    " if is_last else "│   "
                        lines.append(f"  {_C.DIM}  {pad} proof: {proof}{_C.RESET}")

                # 2nd-level callers (transitive)
                if max_depth >= 2:
                    indirect_callers = sorted(reverse.get(caller, set()))
                    for j, caller2 in enumerate(indirect_callers[:5]):
                        is_last2 = j == len(indirect_callers[:5]) - 1
                        indent = "    " if is_last else "│   "
                        prefix2 = "└──" if is_last2 else "├──"
                        lines.append(
                            f"  {_C.DIM}  {indent}{prefix2} {caller2}{_C.RESET}"
                        )
                    if len(indirect_callers) > 5:
                        indent = "    " if is_last else "│   "
                        lines.append(
                            f"  {_C.DIM}  {indent}... +{len(indirect_callers) - 5} more{_C.RESET}"
                        )

        lines.append("")

        # ---- CALLEES (what does this function depend on?) ----
        direct_callees = sorted(graph.get(sym_id, []))
        lines.append(f"  {_C.BOLD}{_C.GREEN}▼ WHAT DOES THIS CALL? (dependencies){_C.RESET}")

        if not direct_callees:
            lines.append(f"  {_C.DIM}  (no outgoing calls resolved){_C.RESET}")
        else:
            for i, callee in enumerate(direct_callees):
                is_last = i == len(direct_callees) - 1
                prefix = "└──" if is_last else "├──"
                lines.append(f"  {_C.GREEN}  {prefix} {callee}{_C.RESET}")

                if show_proof:
                    proof = _find_proof(sym_id, callee, symbols, graph)
                    if proof:
                        pad = "    " if is_last else "│   "
                        lines.append(f"  {_C.DIM}  {pad} proof: {proof}{_C.RESET}")

                # 2nd-level callees
                if max_depth >= 2:
                    indirect_callees = sorted(graph.get(callee, []))
                    for j, callee2 in enumerate(indirect_callees[:5]):
                        is_last2 = j == len(indirect_callees[:5]) - 1
                        indent = "    " if is_last else "│   "
                        prefix2 = "└──" if is_last2 else "├──"
                        lines.append(
                            f"  {_C.DIM}  {indent}{prefix2} {callee2}{_C.RESET}"
                        )
                    if len(indirect_callees) > 5:
                        indent = "    " if is_last else "│   "
                        lines.append(
                            f"  {_C.DIM}  {indent}... +{len(indirect_callees) - 5} more{_C.RESET}"
                        )

        lines.append("")

        # ---- FULL TRANSITIVE BLAST RADIUS ----
        all_affected = _get_transitive_callers(reverse, sym_id, max_depth)
        lines.append(f"  {_C.BOLD}{_C.CYAN}◉ FULL BLAST RADIUS{_C.RESET}")
        lines.append(
            f"  {_C.CYAN}  {len(all_affected)} symbols transitively affected{_C.RESET}"
        )

        # Group by file
        by_file: Dict[str, List[str]] = {}
        for affected_sym in all_affected:
            parts = affected_sym.split(":", 1)
            filepath = parts[0] if len(parts) == 2 else "unknown"
            by_file.setdefault(filepath, []).append(affected_sym)

        for filepath, syms in sorted(by_file.items()):
            lines.append(f"  {_C.BLUE}  📄 {filepath} ({len(syms)} symbols){_C.RESET}")
            for sym in sorted(syms)[:8]:
                name = sym.split(":", 1)[1] if ":" in sym else sym
                lines.append(f"  {_C.DIM}     · {name}{_C.RESET}")
            if len(syms) > 8:
                lines.append(f"  {_C.DIM}     ... +{len(syms) - 8} more{_C.RESET}")

    # ---- SUMMARY ----
    lines.append("")
    lines.append(f"{_C.BOLD}{_C.WHITE}{'─' * 70}{_C.RESET}")

    total_blast = set()
    for sym_id in changed_symbols:
        total_blast.update(_get_transitive_callers(reverse, sym_id, max_depth))

    total_deps = set()
    for sym_id in changed_symbols:
        total_deps.update(_get_transitive_callees(graph, sym_id, max_depth))

    blast_files = set()
    for sym in total_blast:
        parts = sym.split(":", 1)
        if len(parts) == 2:
            blast_files.add(parts[0])

    lines.append(f"  {_C.BOLD}Summary:{_C.RESET}")
    lines.append(f"    Changed symbols    : {_C.RED}{len(changed_symbols)}{_C.RESET}")
    lines.append(f"    Direct callers     : {_C.YELLOW}{sum(len(reverse.get(s, set())) for s in changed_symbols)}{_C.RESET}")
    lines.append(f"    Direct dependencies: {_C.GREEN}{sum(len(graph.get(s, [])) for s in changed_symbols)}{_C.RESET}")
    lines.append(f"    Total blast radius : {_C.CYAN}{len(total_blast)} symbols across {len(blast_files)} files{_C.RESET}")
    lines.append(f"    Total dependencies : {_C.GREEN}{len(total_deps)} symbols{_C.RESET}")
    lines.append(f"{_C.BOLD}{_C.WHITE}{'═' * 70}{_C.RESET}")
    lines.append("")

    return "\n".join(lines)


def render_verification(
    graph: Dict[str, List[str]],
    changed_symbols: List[str],
    symbols: dict,
) -> str:
    """
    Render verification proof: for each edge in the blast radius,
    show the actual code line that creates the dependency.
    """
    reverse: Dict[str, Set[str]] = {}
    for caller, callees in graph.items():
        for callee in callees:
            reverse.setdefault(callee, set()).add(caller)

    lines: List[str] = []
    lines.append("")
    lines.append(f"{_C.BOLD}{_C.WHITE}{'═' * 70}{_C.RESET}")
    lines.append(f"{_C.BOLD}{_C.WHITE}  VERIFICATION: Proof of each connection{_C.RESET}")
    lines.append(f"{_C.BOLD}{_C.WHITE}{'═' * 70}{_C.RESET}")

    for sym_id in changed_symbols:
        lines.append("")
        lines.append(f"  {_C.BOLD}{_C.RED}⚡ {sym_id}{_C.RESET}")
        lines.append("")

        # Verify each caller
        callers = sorted(reverse.get(sym_id, set()))
        if callers:
            lines.append(f"  {_C.BOLD}{_C.YELLOW}  Callers (these functions call the changed code):{_C.RESET}")
            for caller in callers:
                lines.append(f"  {_C.YELLOW}    → {caller}{_C.RESET}")
                proof = _find_proof(caller, sym_id, symbols, graph)
                if proof:
                    lines.append(f"  {_C.DIM}      evidence: {proof}{_C.RESET}")
                else:
                    lines.append(f"  {_C.DIM}      evidence: (edge exists in call graph){_C.RESET}")
            lines.append("")

        # Verify each callee
        callees = sorted(graph.get(sym_id, []))
        if callees:
            lines.append(f"  {_C.BOLD}{_C.GREEN}  Callees (the changed code calls these):{_C.RESET}")
            for callee in callees:
                lines.append(f"  {_C.GREEN}    → {callee}{_C.RESET}")
                proof = _find_proof(sym_id, callee, symbols, graph)
                if proof:
                    lines.append(f"  {_C.DIM}      evidence: {proof}{_C.RESET}")
                else:
                    lines.append(f"  {_C.DIM}      evidence: (edge exists in call graph){_C.RESET}")

    lines.append("")
    lines.append(f"{_C.BOLD}{_C.WHITE}{'═' * 70}{_C.RESET}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_transitive_callers(
    reverse: Dict[str, Set[str]],
    start: str,
    max_depth: int,
) -> List[str]:
    """BFS up the reverse graph to find all transitively affected symbols."""
    visited: Set[str] = {start}
    result: List[str] = []
    frontier = [start]
    depth = 0

    while frontier and depth < max_depth:
        next_frontier = []
        for node in frontier:
            for caller in reverse.get(node, set()):
                if caller not in visited:
                    visited.add(caller)
                    result.append(caller)
                    next_frontier.append(caller)
        frontier = next_frontier
        depth += 1

    return result


def _get_transitive_callees(
    graph: Dict[str, List[str]],
    start: str,
    max_depth: int,
) -> List[str]:
    """BFS down the forward graph to find all dependencies."""
    visited: Set[str] = {start}
    result: List[str] = []
    frontier = [start]
    depth = 0

    while frontier and depth < max_depth:
        next_frontier = []
        for node in frontier:
            for callee in graph.get(node, []):
                if callee not in visited:
                    visited.add(callee)
                    result.append(callee)
                    next_frontier.append(callee)
        frontier = next_frontier
        depth += 1

    return result


def _find_proof(
    caller_id: str,
    callee_id: str,
    symbols: dict,
    graph: Dict[str, List[str]],
) -> Optional[str]:
    """
    Find the actual code line in `caller` that references `callee`.

    This is the "proof" that the edge is real — a grep through the caller's
    source code for the callee's function name.
    """
    if caller_id not in symbols:
        return None

    caller_sym = symbols[caller_id]
    callee_name = callee_id.split(":")[-1] if ":" in callee_id else callee_id

    # Strip class prefix for method calls: "ClassName.method" -> "method"
    if "." in callee_name:
        bare_name = callee_name.split(".")[-1]
    else:
        bare_name = callee_name

    # Search caller's code for the call
    for i, line in enumerate(caller_sym.code.splitlines(), start=1):
        stripped = line.strip()
        # Look for function/method call pattern
        if bare_name + "(" in stripped or f".{bare_name}(" in stripped:
            # Truncate long lines
            if len(stripped) > 80:
                stripped = stripped[:77] + "..."
            return f"line {caller_sym.lineno + i - 1}: {stripped}"

    return None
