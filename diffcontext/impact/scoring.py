"""
scoring.py — Impact scoring for prioritizing symbols in context.

Algorithm: Bidirectional decay propagation from changed symbols.

Forward  (callees): score decays by CALLEE_DECAY per hop
Backward (callers): score decays by CALLER_DECAY per hop

Structural bonuses:
  - Sibling bonus: shares a caller with a changed symbol (co-change signal),
    weighted by 1/caller_outdegree so hub callers don’t flood the pool,
    and log-scaled before capping to dampen compounding across many hubs.
  - Structural bonus: log2(1 + indegree)*3 + log2(1 + outdegree),
    hard-capped at STRUCT_MAX so mega-hub nodes (outdegree=400) don’t
    accumulate +800 and crowd out genuinely co-changed code.

Changes vs v1:
  1. Structural bonus capped (STRUCT_MAX=15). Uncapped bonus turned hubs
     into permanent top-scorers regardless of actual co-change signal.
  2. BFS propagation cutoff raised 5.0 → 8.0. Large repos (transformers)
     have long paths; 5.0 prematurely cut valid propagation chains.
  3. Sibling bonus log-scaled before accumulation to dampen compounding.
"""

import math
from collections import deque
from typing import Dict, List, Optional, Set


# Tunable constants
CHANGED_SCORE   = 100.0
CALLEE_DECAY    = 0.65   # lowered: callees are less likely to co-change
CALLER_DECAY    = 0.85   # raised: callers co-change more than callees
CALLEE_BASE     = 90.0   # direct callee of changed symbol
CALLER_BASE     = 85.0   # direct caller
SIBLING_BASE    = 60.0   # base for sibling contribution (divided by caller_outdegree)
SIBLING_MAX     = 80.0   # cap: a symbol can’t accumulate more than this from siblings
BLAST_BASE      = 30.0   # in blast_radii but not reached by BFS
MAX_HOPS        = 8      # propagation depth limit
BFS_CUTOFF      = 8.0    # stop propagating a path when score drops below this
STRUCT_MAX      = 15.0   # hard cap on structural bonus (log-scaled)


def compute_impact_scores(
    graph: Dict[str, List[str]],
    changed_symbols: List[str],
    blast_radii: Dict[str, List[str]],
    expanded_deps: List[str] = None,
    reverse: Optional[Dict[str, Set[str]]] = None,
) -> Dict[str, float]:
    """
    Score every symbol's relevance to understanding the change.

    Args:
        graph:           Forward call graph.
        changed_symbols: Symbols that were modified.
        blast_radii:     Pre-computed blast radii per changed symbol.
        expanded_deps:   Symbols reachable by forward dependency expansion.
        reverse:         Pre-built reverse graph (built internally if None).

    Returns dict of symbol_id -> score (higher = more important).
    """
    # Build reverse graph once (or reuse caller's)
    if reverse is None:
        reverse = {}
        for caller, callees in graph.items():
            for callee in callees:
                reverse.setdefault(callee, set()).add(caller)

    changed_set = set(changed_symbols)
    scores: Dict[str, float] = {}

    # ── 1. Changed symbols = 100 ──────────────────────────────────────────
    for sym in changed_symbols:
        scores[sym] = CHANGED_SCORE

    # ── 2. Forward BFS (callees) with decay ──────────────────────────────
    queue: deque = deque()
    for sym in changed_symbols:
        for callee in graph.get(sym, []):
            if callee not in changed_set:
                queue.append((callee, CALLEE_BASE, 1))

    visited_fwd: Set[str] = set(changed_symbols)
    while queue:
        node, score, hop = queue.popleft()
        if node in visited_fwd or hop > MAX_HOPS:
            continue
        visited_fwd.add(node)
        scores[node] = max(scores.get(node, 0.0), score)
        next_score = score * CALLEE_DECAY
        if next_score >= BFS_CUTOFF:
            for callee in graph.get(node, []):
                if callee not in visited_fwd:
                    queue.append((callee, next_score, hop + 1))

    # ── 3. Backward BFS (callers) with decay ─────────────────────────────
    queue2: deque = deque()
    for sym in changed_symbols:
        for caller in reverse.get(sym, set()):
            if caller not in changed_set:
                queue2.append((caller, CALLER_BASE, 1))

    visited_bwd: Set[str] = set(changed_symbols)
    while queue2:
        node, score, hop = queue2.popleft()
        if node in visited_bwd or hop > MAX_HOPS:
            continue
        visited_bwd.add(node)
        scores[node] = max(scores.get(node, 0.0), score)
        next_score = score * CALLER_DECAY
        if next_score >= BFS_CUTOFF:
            for caller in reverse.get(node, set()):
                if caller not in visited_bwd:
                    queue2.append((caller, next_score, hop + 1))

    # ── 4. Specificity-weighted sibling bonus ────────────────────────────
    sibling_accumulator: Dict[str, float] = {}

    for sym in changed_symbols:
        for caller in reverse.get(sym, set()):
            caller_callees = graph.get(caller, [])
            caller_outdegree = len(caller_callees)
            if caller_outdegree <= 1:
                continue
            raw_contribution = SIBLING_BASE / caller_outdegree
            contribution = math.log2(1.0 + raw_contribution)
            for sibling in caller_callees:
                if sibling not in changed_set and sibling != sym:
                    sibling_accumulator[sibling] = (
                        sibling_accumulator.get(sibling, 0.0) + contribution
                    )

    for sibling, bonus in sibling_accumulator.items():
        capped_bonus = min(bonus, SIBLING_MAX)
        scores[sibling] = scores.get(sibling, 0.0) + capped_bonus

    # ── 5. Expanded deps: give them a meaningful score ────────────────────
    if expanded_deps:
        for sym in expanded_deps:
            if sym not in scores:
                scores[sym] = BLAST_BASE

    # ── 6. Remaining blast radius symbols not yet scored ─────────────────
    for sym, radius in blast_radii.items():
        for affected in radius:
            if affected not in scores:
                scores[affected] = BLAST_BASE

    # ── 7. Structural bonus: log-scaled, hard-capped ──────────────────────────
    # Previous formula (indegree*2 + outdegree) was unbounded: a hub with
    # indegree=50 got +100 structural bonus, drowning all co-change signal.
    # log2(1+degree) grows slowly and the STRUCT_MAX cap prevents run-away.
    for sym in scores:
        indegree  = len(reverse.get(sym, set()))
        outdegree = len(graph.get(sym, []))
        struct_bonus = math.log2(1 + indegree) * 3 + math.log2(1 + outdegree)
        scores[sym] += min(struct_bonus, STRUCT_MAX)

    return scores