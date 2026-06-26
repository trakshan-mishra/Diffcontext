"""
blast_radius.py — Find all symbols transitively affected by a change.

Given a changed symbol, walks the REVERSE graph (callers) to find
everything that depends on it.

Performance fix: accept an optional pre-built reverse graph so the caller
can build it once and reuse it across multiple symbols instead of
rebuilding it O(N) times.
"""

from typing import Dict, List, Optional, Set


def build_reverse_graph(graph: Dict[str, List[str]]) -> Dict[str, Set[str]]:
    """Build the reverse (caller) graph once; reuse for many queries."""
    reverse: Dict[str, Set[str]] = {}
    for caller, callees in graph.items():
        for callee in callees:
            reverse.setdefault(callee, set()).add(caller)
    return reverse


def get_blast_radius(
    graph: Dict[str, List[str]],
    changed_symbol: str,
    reverse: Optional[Dict[str, Set[str]]] = None,
) -> List[str]:
    """
    All functions that (transitively) call changed_symbol.

    Args:
        graph:          Forward call graph (caller -> [callees]).
        changed_symbol: The symbol whose blast radius we want.
        reverse:        Pre-built reverse graph. If None it is built here
                        (correct but wasteful when called in a loop).

    Uses iterative DFS with cycle detection.
    """
    if reverse is None:
        reverse = build_reverse_graph(graph)

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
