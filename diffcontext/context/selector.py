"""
selector.py — Select which symbols to include in context, respecting token budget.
"""

from typing import Dict, List, Optional, Tuple

from ..models import Symbol


def select_context(
    symbols: Dict[str, Symbol],
    scores: Dict[str, float],
    changed: List[str],
    max_tokens: Optional[int] = None,
) -> Tuple[List[str], List[str]]:
    """
    Select symbols for context based on scores and token budget.

    Priority:
    1. Changed symbols (always included)
    2. Score >= 80 (direct relationships - always included)
    3. Remaining by score until token budget exhausted

    Returns:
        (selected_ids, dropped_ids)
        dropped_ids: scored symbols that exist in `symbols` but were cut by
        the token budget. The LLM is explicitly told about these so it knows
        its blind spots rather than assuming the graph is complete.
    """
    if not scores:
        return changed, []

    # Sort by score descending
    scored = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    changed_set = set(changed)
    result: List[str] = []
    dropped: List[str] = []
    current_tokens = 0

    for sym_id, score in scored:
        if sym_id not in symbols:
            continue

        sym_tokens = _estimate_tokens(symbols[sym_id].code)

        # Always include changed symbols and high-relevance
        if sym_id in changed_set or score >= 80:
            result.append(sym_id)
            current_tokens += sym_tokens
            continue

        # Apply token budget for lower-relevance
        if max_tokens is not None and current_tokens + sym_tokens > max_tokens:
            dropped.append(sym_id)
            continue

        result.append(sym_id)
        current_tokens += sym_tokens

    return result, dropped


def _estimate_tokens(text: str) -> int:
    """~4 chars per token (GPT approximation). Add 20% buffer for safety."""
    return max(1, int(len(text) / 4 * 1.2))