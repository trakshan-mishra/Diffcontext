"""
selector.py — Select which symbols to include in context, respecting token budget.

Key fix over original:
  - Changed symbols are the ONLY unconditional include.
  - Score >= 80 threshold no longer bypasses the token budget.
    That rule caused direct callees (score ~90 after structural bonus)
    to consume the entire budget before any co-change sibling candidates
    (score ~38-50) were even evaluated. Precision suffered badly.
  - Instead: rank strictly by score, apply budget universally, with one
    exception: changed symbols always fit (they're the reason we're here).
  - Added a per-symbol token cap so one giant function can't crowd out
    ten relevant small ones.

Fix vs previous version:
  The token cap logic was wrong: it counted a capped amount toward the
  budget (250 tokens) but included the full symbol in the result, causing
  silent overruns. The correct behavior is: if a symbol exceeds the cap,
  SKIP IT ENTIRELY rather than including it at a lie. This means the
  selector tries smaller candidates next instead of filling context with
  one huge function and then claiming there's room for more.

Fix vs previous version (token-accounting mismatch):
  The selector used to budget on token_count(symbol.code) — the bare
  function body — while the compiler renders each symbol with a FILE:/
  FUNCTION: header and a CALLERS/CALLEES relationship block on top of the
  code, and reports tokens over that full rendered block. The gap between
  what was budgeted and what was emitted produced a systematic 25-41%
  overshoot of --max-tokens (reproduced on psf/black at every budget from
  500 to 8000). Now, when the caller passes the call graph, each candidate
  is measured with compiler.render_symbol_block() — the exact rendering the
  compiler will emit — using a pessimistic empty selected_set so every
  relationship entry counts the longer " [NOT IN CONTEXT]" tag. Without a
  graph the old code-only behavior is preserved so existing library callers
  don't silently change.
"""

from typing import Callable, Dict, List, Optional, Set, Tuple

from ..models import Symbol
from .compiler import build_reverse_graph, relationship_cap, render_symbol_block


# A single symbol can burn at most this fraction of the total budget.
# Prevents one huge function from crowding out ten relevant small ones.
MAX_SINGLE_SYMBOL_FRACTION = 0.25

# Largest-gap cutoff ("gap50" in the benchmarks): the relative-drop search
# window and the score floor below which a candidate is not retrievable at
# all. Both values are the exact ones measured in blend_loro.py
# eval_cutoff_policies (benchmarks/RIGOR_REPORT_2026-07.md §7).
GAP_CUTOFF_WINDOW = 50
GAP_SCORE_EPSILON = 1e-12


def gap_cut_count(ranked_scores: List[float]) -> int:
    """
    How many leading candidates the largest-gap cutoff keeps.

    `ranked_scores` must be positive scores sorted descending. The cut lands
    at the largest relative drop (score[i] / score[i+1]) between consecutive
    candidates within the first GAP_CUTOFF_WINDOW — the policy measured
    F1-optimal on all five benchmark repos: ~4x the precision of fixed
    top-20 at 6-9 retrieved symbols, for ~30% relative recall cost
    (benchmarks/RIGOR_REPORT_2026-07.md §7). With fewer than 3 candidates
    there is no distribution to read, so everything is kept.
    """
    n = len(ranked_scores)
    if n < 3:
        return n
    head = ranked_scores[:GAP_CUTOFF_WINDOW]
    best_i, best_ratio = 1, 0.0
    for i in range(len(head) - 1):
        ratio = head[i] / max(head[i + 1], GAP_SCORE_EPSILON)
        if ratio > best_ratio:
            best_ratio = ratio
            best_i = i + 1
    return best_i


def select_context(
    symbols: Dict[str, Symbol],
    scores: Dict[str, float],
    changed: List[str],
    max_tokens: Optional[int] = None,
    token_counter: Optional[Callable[[str], int]] = None,
    top_k: Optional[int] = None,
    graph: Optional[Dict[str, List[str]]] = None,
    reverse: Optional[Dict[str, Set[str]]] = None,
    rel_cap: Optional[int] = None,
    cutoff: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
    """
    Select symbols for context based on scores and token budget.

    Priority:
      1. Changed symbols always included (no budget bypass for anything else)
      2. All remaining symbols ranked by score, included until budget exhausted

    Args:
        token_counter: Optional callable text -> token count. Pass your
            model's real tokenizer (e.g. tiktoken, Anthropic counting) when
            enforcing a hard context-window limit; defaults to the
            ~4-chars-per-token heuristic, which is approximate.
        top_k: Optional cap on the number of NON-changed symbols included,
            applied on top of the token budget. The eval_v2 benchmark found
            retrieval recall plateaus around 20 symbols per changed symbol
            while precision keeps degrading, so a caller optimizing for
            signal-to-noise should pass ~20 * len(changed).
        graph: Optional call graph (id -> [dep ids]). When provided, each
            candidate is budgeted at its FULL rendered size (headers +
            relationship annotations + code) via render_symbol_block, which
            is what the compiler actually emits. Omit for the legacy
            code-only accounting (kept for backward compatibility, but it
            undercounts and the compiled output will overshoot the budget).
        reverse: Optional precomputed reverse graph (callee -> callers).
            Derived from `graph` when absent.
        rel_cap: Relationship-block entry cap used for size measurement;
            defaults to compiler.relationship_cap(max_tokens) so selector
            and compiler always measure the same rendering.
        cutoff: Optional dynamic cutoff policy applied to the score ranking
            BEFORE top_k and the token budget (the order the benchmark
            measured). "gap" cuts at the largest relative score drop
            (see gap_cut_count) and additionally drops zero-score
            candidates, which the policy never retrieves. None/"topk"
            keeps today's recall-first behavior. Note: the benchmark
            measured the policy per single-changed-symbol query; with
            multiple changed symbols it applies to the merged ranking.

    Returns:
        (selected_ids, dropped_ids)
        dropped_ids: scored symbols that exist in `symbols` but were cut by
        the token budget. The LLM is told about these explicitly.
    """
    count = token_counter or _estimate_tokens

    if cutoff not in (None, "topk", "gap"):
        raise ValueError(
            f"unknown cutoff policy {cutoff!r} — expected 'gap', 'topk', or None"
        )

    if not scores:
        return list(changed), []

    if graph is not None and reverse is None:
        reverse = build_reverse_graph(graph)
    if rel_cap is None:
        rel_cap = relationship_cap(max_tokens)

    def rendered_size(sym_id: str) -> int:
        """Tokens this symbol will actually cost in the compiled output."""
        if graph is None:
            return count(symbols[sym_id].code)
        # Empty selected_set = every relationship entry gets the longer
        # " [NOT IN CONTEXT]" tag = safe upper bound on the real rendering.
        return count(render_symbol_block(
            sym_id, symbols, scores.get(sym_id, 0), graph, reverse,
            set(), rel_cap=rel_cap,
        ))

    per_sym_cap = int(max_tokens * MAX_SINGLE_SYMBOL_FRACTION) if max_tokens else None

    changed_set = set(changed)
    result: List[str] = []
    dropped: List[str] = []
    current_tokens = 0

    # ── Pass 1: changed symbols always in, no budget check ───────────────
    for sym_id in changed:
        if sym_id in symbols:
            result.append(sym_id)
            current_tokens += rendered_size(sym_id)

    # ── Pass 2: everything else ranked by score, budget-gated ────────────
    scored = sorted(
        ((sid, sc) for sid, sc in scores.items() if sid not in changed_set),
        key=lambda x: x[1],
        reverse=True,
    )

    gap_kept: Optional[Set[str]] = None
    if cutoff == "gap":
        candidates = [
            (sid, sc) for sid, sc in scored
            if sid in symbols and sc > GAP_SCORE_EPSILON
        ]
        keep_n = gap_cut_count([sc for _, sc in candidates])
        gap_kept = {sid for sid, _ in candidates[:keep_n]}

    included_non_changed = 0
    for sym_id, score in scored:
        if sym_id not in symbols:
            continue

        if gap_kept is not None and sym_id not in gap_kept:
            dropped.append(sym_id)
            continue

        if top_k is not None and included_non_changed >= top_k:
            dropped.append(sym_id)
            continue

        sym_tokens = rendered_size(sym_id)

        # FIX: if the symbol exceeds the per-symbol cap, skip it entirely.
        # Previous code counted the capped amount toward the budget but
        # still included the full symbol, silently overrunning the budget
        # and then continuing to include more symbols as if there were room.
        # Skipping is correct: the budget should gate what's actually included.
        if per_sym_cap is not None and sym_tokens > per_sym_cap:
            dropped.append(sym_id)
            continue

        if max_tokens is not None and current_tokens + sym_tokens > max_tokens:
            dropped.append(sym_id)
            continue

        result.append(sym_id)
        included_non_changed += 1
        current_tokens += sym_tokens

    return result, dropped


def _estimate_tokens(text: str) -> int:
    """~4 chars per token (GPT approximation). Add 20% buffer for safety."""
    return max(1, int(len(text) / 4 * 1.2))
