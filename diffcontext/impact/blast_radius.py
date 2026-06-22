"""
blast_radius.py — Find all symbols transitively affected by a change.

Given a changed symbol, walks the REVERSE graph (callers) to find
everything that depends on it.
"""

from typing import Dict, List, Set


def get_blast_radius(
    graph: Dict[str, List[str]],
    changed_symbol: str,
) -> List[str]:
    """
    All functions that (transitively) call changed_symbol.


    
    Uses iterative DFS with cycle detection.
    """
    # Build reverse graph
    reverse: Dict[str, Set[str]] = {}
    for caller, callees in graph.items():
        for callee in callees:
            reverse.setdefault(callee, set()).add(caller)

    affected: List[str] = []
    visited: Set[str] = set()

    # Iterative DFS on reverse graph
    stack = [changed_symbol]
    visited.add(changed_symbol)

    while stack:
        current = stack.pop()
        for caller in reverse.get(current, ()):
            if caller not in visited:
                visited.add(caller)
                affected.append(caller)
                stack.append(caller)

    return affected
