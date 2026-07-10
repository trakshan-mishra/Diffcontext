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
"""

from typing import Callable, Dict, List, Optional, Tuple

from ..models import Symbol


# A single symbol can burn at most this fraction of the total budget.
# Prevents one huge function from crowding out ten relevant small ones.
MAX_SINGLE_SYMBOL_FRACTION = 0.25


def select_context(
    symbols: Dict[str, Symbol],
    scores: Dict[str, float],
    changed: List[str],
    max_tokens: Optional[int] = None,
    token_counter: Optional[Callable[[str], int]] = None,
    top_k: Optional[int] = None,
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

    Returns:
        (selected_ids, dropped_ids)
        dropped_ids: scored symbols that exist in `symbols` but were cut by
        the token budget. The LLM is told about these explicitly.
    """
    count = token_counter or _estimate_tokens

    if not scores:
        return list(changed), []

    per_sym_cap = int(max_tokens * MAX_SINGLE_SYMBOL_FRACTION) if max_tokens else None

    changed_set = set(changed)
    result: List[str] = []
    dropped: List[str] = []
    current_tokens = 0

    # ── Pass 1: changed symbols always in, no budget check ───────────────
    for sym_id in changed:
        if sym_id in symbols:
            result.append(sym_id)
            current_tokens += count(symbols[sym_id].code)

    # ── Pass 2: everything else ranked by score, budget-gated ────────────
    scored = sorted(
        ((sid, sc) for sid, sc in scores.items() if sid not in changed_set),
        key=lambda x: x[1],
        reverse=True,
    )

    included_non_changed = 0
    for sym_id, score in scored:
        if sym_id not in symbols:
            continue

        if top_k is not None and included_non_changed >= top_k:
            dropped.append(sym_id)
            continue

        sym_tokens = count(symbols[sym_id].code)

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