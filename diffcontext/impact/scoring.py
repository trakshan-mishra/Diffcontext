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
from dataclasses import dataclass
from typing import Dict, List, Optional, Set


# Tunable constants — the module-level values are the tuned defaults; pass a
# ScoringConfig to compute_impact_scores() to override per call instead of
# editing these.
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


@dataclass(frozen=True)
class ScoringConfig:
    """
    Tunable weights for impact scoring. A harness running different loop
    kinds (broad refactor check vs. narrow bug fix) can pass its own config
    instead of editing module constants; benchmark ablations can sweep
    configs without touching source.

    Defaults are read from the module-level constants at construction time,
    so existing tuning (and tests that monkeypatch the constants) keep
    working unchanged.
    """
    changed_score: float = None   # type: ignore[assignment]
    callee_decay: float = None    # type: ignore[assignment]
    caller_decay: float = None    # type: ignore[assignment]
    callee_base: float = None     # type: ignore[assignment]
    caller_base: float = None     # type: ignore[assignment]
    sibling_base: float = None    # type: ignore[assignment]
    sibling_max: float = None     # type: ignore[assignment]
    blast_base: float = None      # type: ignore[assignment]
    max_hops: int = None          # type: ignore[assignment]
    bfs_cutoff: float = None      # type: ignore[assignment]
    struct_max: float = None      # type: ignore[assignment]

    def __post_init__(self):
        defaults = {
            "changed_score": CHANGED_SCORE, "callee_decay": CALLEE_DECAY,
            "caller_decay": CALLER_DECAY, "callee_base": CALLEE_BASE,
            "caller_base": CALLER_BASE, "sibling_base": SIBLING_BASE,
            "sibling_max": SIBLING_MAX, "blast_base": BLAST_BASE,
            "max_hops": MAX_HOPS, "bfs_cutoff": BFS_CUTOFF,
            "struct_max": STRUCT_MAX,
        }
        for name, default in defaults.items():
            if getattr(self, name) is None:
                object.__setattr__(self, name, default)


def describe_scoring_basis(config: "Optional[ScoringConfig]" = None) -> str:
    """
    One-line, human/LLM-readable summary of the live scoring parameters.

    Consumed by the context compiler's meta-header so the description can
    never drift from the actual algorithm again (it used to be hardcoded
    prose that went stale when constants changed). Pass the same
    ScoringConfig used for scoring to describe a non-default run.
    """
    cfg = config if config is not None else ScoringConfig()
    return (
        f"changed={cfg.changed_score:.0f} "
        f"| direct_callee={cfg.callee_base:.0f} | direct_caller={cfg.caller_base:.0f} "
        f"| 2hop_callee={cfg.callee_base * cfg.callee_decay:.1f} "
        f"| 2hop_caller={cfg.caller_base * cfg.caller_decay:.1f} "
        f"| struct bonus=log2-scaled, capped at {cfg.struct_max:.0f}"
    )


def compute_impact_scores(
    graph: Dict[str, List[str]],
    changed_symbols: List[str],
    blast_radii: Dict[str, List[str]],
    expanded_deps: List[str] = None,
    reverse: Optional[Dict[str, Set[str]]] = None,
    config: Optional[ScoringConfig] = None,
) -> Dict[str, float]:
    """
    Score every symbol's relevance to understanding the change.

    Args:
        graph:           Forward call graph.
        changed_symbols: Symbols that were modified.
        blast_radii:     Pre-computed blast radii per changed symbol.
        expanded_deps:   Symbols reachable by forward dependency expansion.
        reverse:         Pre-built reverse graph (built internally if None).
        config:          Scoring weights; module-constant defaults if None.

    Returns dict of symbol_id -> score (higher = more important).
    """
    cfg = config if config is not None else ScoringConfig()
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
        scores[sym] = cfg.changed_score

    # ── 2. Forward BFS (callees) with decay ──────────────────────────────
    queue: deque = deque()
    for sym in changed_symbols:
        for callee in graph.get(sym, []):
            if callee not in changed_set:
                queue.append((callee, cfg.callee_base, 1))

    visited_fwd: Set[str] = set(changed_symbols)
    while queue:
        node, score, hop = queue.popleft()
        if node in visited_fwd or hop > cfg.max_hops:
            continue
        visited_fwd.add(node)
        scores[node] = max(scores.get(node, 0.0), score)
        next_score = score * cfg.callee_decay
        if next_score >= cfg.bfs_cutoff:
            for callee in graph.get(node, []):
                if callee not in visited_fwd:
                    queue.append((callee, next_score, hop + 1))

    # ── 3. Backward BFS (callers) with decay ─────────────────────────────
    queue2: deque = deque()
    for sym in changed_symbols:
        for caller in reverse.get(sym, set()):
            if caller not in changed_set:
                queue2.append((caller, cfg.caller_base, 1))

    visited_bwd: Set[str] = set(changed_symbols)
    while queue2:
        node, score, hop = queue2.popleft()
        if node in visited_bwd or hop > cfg.max_hops:
            continue
        visited_bwd.add(node)
        scores[node] = max(scores.get(node, 0.0), score)
        next_score = score * cfg.caller_decay
        if next_score >= cfg.bfs_cutoff:
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
            raw_contribution = cfg.sibling_base / caller_outdegree
            contribution = math.log2(1.0 + raw_contribution)
            for sibling in caller_callees:
                if sibling not in changed_set and sibling != sym:
                    sibling_accumulator[sibling] = (
                        sibling_accumulator.get(sibling, 0.0) + contribution
                    )

    for sibling, bonus in sibling_accumulator.items():
        capped_bonus = min(bonus, cfg.sibling_max)
        scores[sibling] = scores.get(sibling, 0.0) + capped_bonus

    # ── 5. Expanded deps: give them a meaningful score ────────────────────
    if expanded_deps:
        for sym in expanded_deps:
            if sym not in scores:
                scores[sym] = cfg.blast_base

    # ── 6. Remaining blast radius symbols not yet scored ─────────────────
    for sym, radius in blast_radii.items():
        for affected in radius:
            if affected not in scores:
                scores[affected] = cfg.blast_base

    # ── 7. Structural bonus: log-scaled, hard-capped ──────────────────────────
    # Previous formula (indegree*2 + outdegree) was unbounded: a hub with
    # indegree=50 got +100 structural bonus, drowning all co-change signal.
    # log2(1+degree) grows slowly and the cfg.struct_max cap prevents run-away.
    for sym in scores:
        indegree  = len(reverse.get(sym, set()))
        outdegree = len(graph.get(sym, []))
        struct_bonus = math.log2(1 + indegree) * 3 + math.log2(1 + outdegree)
        scores[sym] += min(struct_bonus, cfg.struct_max)

    return scores