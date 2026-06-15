"""
relevance_scorer.py - Score relevance of functions for context selection.

Higher score = more relevant to understanding the change.
"""

from typing import List, Dict, Set


def score_relevance(
    graph: Dict[str, List[str]],
    changed_functions: List[str],
    retrieved_function: str
) -> int:
    """
    Score a function's relevance to understanding a change.
    
    Returns score from 0-100:
    - 100: The changed function itself
    - 90: Direct callees of changed function
    - 80: Direct callers of changed function (blast radius)
    - 60: 2-hop callees
    - 50: 2-hop callers
    - 30: Indirect dependencies (3+ hops)
    - 0: Unrelated
    """
    changed_set = set(changed_functions)
    
    # Changed function itself
    if retrieved_function in changed_set:
        return 100
    
    # Build reverse index for caller lookups
    reverse = {}
    for caller, callees in graph.items():
        for callee in callees:
            reverse.setdefault(callee, set()).add(caller)
    
    min_distance = float('inf')
    
    for changed in changed_set:
        # Check if direct callee
        if retrieved_function in graph.get(changed, []):
            min_distance = min(min_distance, 1)
            continue
        
        # Check if direct caller
        if retrieved_function in reverse.get(changed, set()):
            min_distance = min(min_distance, 1)
            continue
        
        # Check 2-hop callee (changed -> intermediate -> retrieved)
        for intermediate in graph.get(changed, []):
            if retrieved_function in graph.get(intermediate, []):
                min_distance = min(min_distance, 2)
                break
        
        # Check 2-hop caller (caller -> changed -> retrieved)
        for caller in reverse.get(changed, set()):
            if retrieved_function in graph.get(caller, []):
                min_distance = min(min_distance, 2)
                break
        
        # Check 2-hop via reverse (changed <- intermediate <- retrieved)
        for intermediate in reverse.get(changed, set()):
            if retrieved_function in reverse.get(intermediate, set()):
                min_distance = min(min_distance, 2)
                break
        
        # If we found a path but not categorized, it's indirect
        if min_distance < float('inf') and min_distance > 2:
            min_distance = 3
    
    # Score based on distance
    if min_distance == 1:
        return 85  # Direct relationship
    elif min_distance == 2:
        return 60  # One hop away
    elif min_distance == 3:
        return 30  # Indirect
    else:
        return 0   # Unrelated


def rank_by_relevance(
    graph: Dict[str, List[str]],
    changed_functions: List[str],
    candidates: List[str]
) -> List[tuple]:
    """
    Rank candidates by relevance score.
    
    Returns list of (function_id, score) sorted by score descending.
    """
    scored = [
        (fn, score_relevance(graph, changed_functions, fn))
        for fn in candidates
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored