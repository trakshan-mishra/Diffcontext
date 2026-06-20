"""
scoring.py — Impact scoring for prioritizing symbols in context.

score = blast_radius_size * 3 + indegree * 2 + outdegree
"""

from typing import Dict, List, Set


def compute_impact_scores(
    graph: Dict[str, List[str]],
    changed_symbols: List[str],
    blast_radii: Dict[str, List[str]],
) -> Dict[str, float]:
    """
    Score every symbol's relevance to understanding the change.

    Returns dict of symbol_id -> score (higher = more important).

    Scoring:
    - 100: Changed symbol itself
    - 90:  Direct callee of changed symbol
    - 80:  Direct caller (in blast radius)
    - 60:  2-hop callee
    - 50:  2-hop caller
    - Plus structural bonuses: indegree * 2 + outdegree
    """
    # Build reverse graph
    reverse: Dict[str, Set[str]] = {}
    for caller, callees in graph.items():
        for callee in callees:
            reverse.setdefault(callee, set()).add(caller)

    changed_set = set(changed_symbols)
    scores: Dict[str, float] = {}

    # 1. Changed symbols = 100
    for sym in changed_symbols:
        scores[sym] = 100.0

    # 2. Direct callees = 90
    for sym in changed_symbols:
        for callee in graph.get(sym, []):
            if callee not in changed_set:
                scores[callee] = max(scores.get(callee, 0), 90.0)

    # 3. Direct callers (blast radius depth=1) = 80
    for sym in changed_symbols:
        for caller in reverse.get(sym, set()):
            if caller not in changed_set:
                scores[caller] = max(scores.get(caller, 0), 80.0)

    # 4. 2-hop callees = 60
    for sym in changed_symbols:
        for callee in graph.get(sym, []):
            for callee2 in graph.get(callee, []):
                if callee2 not in scores:
                    scores[callee2] = max(scores.get(callee2, 0), 60.0)

    # 5. 2-hop callers = 50
    for sym in changed_symbols:
        for caller in reverse.get(sym, set()):
            for caller2 in reverse.get(caller, set()):
                if caller2 not in scores:
                    scores[caller2] = max(scores.get(caller2, 0), 50.0)

    # 6. Remaining blast radius symbols = 30
    for sym, radius in blast_radii.items():
        for affected in radius:
            if affected not in scores:
                scores[affected] = 30.0

    # 7. Structural bonus: indegree * 2 + outdegree
    for sym in scores:
        indegree = len(reverse.get(sym, set()))
        outdegree = len(graph.get(sym, []))
        scores[sym] += indegree * 2 + outdegree

    return scores
