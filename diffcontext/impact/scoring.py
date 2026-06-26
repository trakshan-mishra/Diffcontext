"""
scoring.py — Impact scoring for prioritizing symbols in context.

Algorithm: Bidirectional decay propagation from changed symbols.

Instead of flat score tiers (100/90/80/60/50), we do a proper
weighted BFS in both directions simultaneously:

  Forward  (callees): score decays by CALLEE_DECAY per hop
  Backward (callers): score decays by CALLER_DECAY per hop

Then add two structural bonuses:
  - Sibling bonus: shares a caller with a changed symbol (co-change signal)
  - Structural bonus: indegree * 2 + outdegree (connectivity weight)

Why this beats flat tiers:
  - Flat tiers cap at 2 hops. Decay propagates as far as the graph goes,
    just with diminishing weight. A 3-hop caller at 0.45 still beats a
    random symbol at 0.
  - Siblings (shared callers) are the #1 co-change predictor. We give them
    SIBLING_BASE (45) as an ADDITIVE bonus on top of any BFS score — the
    old max() approach suppressed this signal whenever BFS had already
    visited the node at a higher score.
  - Decay is multiplicative, so a high-indegree hub at hop 2 beats a
    low-indegree leaf at hop 1. Flat tiers can't express this.
"""

from collections import deque
from typing import Dict, List, Optional, Set


# Tunable constants
CHANGED_SCORE   = 100.0
CALLEE_DECAY    = 0.75   # each callee hop multiplies score by this
CALLER_DECAY    = 0.80   # each caller hop multiplies score by this
CALLEE_BASE     = 90.0   # direct callee of changed symbol
CALLER_BASE     = 85.0   # direct caller (raised: callers co-change more than callees)
SIBLING_BASE    = 45.0   # ADDITIVE: shares a caller with changed (strongest co-change signal)
BLAST_BASE      = 30.0   # in blast_radii but not reached by BFS
MAX_HOPS        = 8      # propagation depth limit (raised from 6)


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
    # Queue entries: (symbol, current_score, hop)
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
        if next_score >= 5.0:  # prune negligible scores
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
        if next_score >= 5.0:
            for caller in reverse.get(node, set()):
                if caller not in visited_bwd:
                    queue2.append((caller, next_score, hop + 1))

    # ── 4. Sibling bonus (ADDITIVE co-change signal) ──────────────────────
    # A sibling shares at least one caller with a changed symbol.
    # These are the strongest co-change candidates.
    # Use ADDITIVE bonus: a sibling already scored 40 by BFS gets +45 = 85,
    # beating an unrelated callee at 90 × 0.75 = 67. This correctly reflects
    # that "changed together" is stronger evidence than "called together".
    for sym in changed_symbols:
        for caller in reverse.get(sym, set()):
            for sibling in graph.get(caller, []):
                if sibling not in changed_set:
                    scores[sibling] = scores.get(sibling, 0.0) + SIBLING_BASE

    # ── 5. Expanded deps: give them a meaningful score ────────────────────
    # Previously expanded_deps was always passed as None from evaluator.py,
    # so these symbols were never scored and silently dropped. Fixed in caller.
    if expanded_deps:
        for sym in expanded_deps:
            if sym not in scores:
                scores[sym] = BLAST_BASE

    # ── 6. Remaining blast radius symbols not yet scored ─────────────────
    for sym, radius in blast_radii.items():
        for affected in radius:
            if affected not in scores:
                scores[affected] = BLAST_BASE

    # ── 7. Structural bonus: indegree * 2 + outdegree ────────────────────
    for sym in scores:
        indegree  = len(reverse.get(sym, set()))
        outdegree = len(graph.get(sym, []))
        scores[sym] += indegree * 2 + outdegree

    return scores