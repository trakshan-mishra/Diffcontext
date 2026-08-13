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
# Prevents one huge LOW-RANKED function from crowding out ten relevant small
# ones. See CAP_EXEMPT_TOP_N for why "low-ranked" is load-bearing.
MAX_SINGLE_SYMBOL_FRACTION = 0.25

# The top N ranked candidates are exempt from the size cap (they remain
# subject to the token budget like everything else).
#
# Size is not independent of relevance: the function everything calls tends to
# BE the big orchestrator, so a score-blind ceiling cuts hardest exactly where
# retrieval matters. On google-api-python-client the unexempted cap evicted
# symbols this ranker had placed 2nd, 2nd, 4th, 6th, 7th, 11th and 15th of 347.
#
# What the exemption actually buys, over 9 repos / 1,102 co-change cases
# (benchmarks/measure_cap_exemption.py --cases 150 --budget 1500 10000):
#
#     budget   recall (on -> top-10)   prec_lb (on -> top-10)
#      1,500     15.3% -> 16.7%          26.0% -> 40.3%
#     10,000     58.2% -> 58.3%          16.0% -> 16.2%
#
# The win is PRECISION UNDER BUDGET PRESSURE. At 1,500 precision gains ~14
# points while spending FEWER tokens (1,440 -> 1,367): it trades several small
# marginal symbols for one large highly-ranked one. At the 10k default the
# policy is nearly irrelevant — 679 vs 682 of 1,102 cases pass — because the
# cap seldom binds when the budget is roomy, and the loop below already skips
# an oversized symbol and keeps scanning, so smaller candidates never lost
# their chance.
#
# Measure at --cases 150, never the old default of 20. At 20/repo this effect
# estimate was noise-inflated: it showed a 1,500-budget recall LOSS that more
# data reversed. The same underpowering flattered the reranker A/B by ~4x, so
# treat any 20-case number in this repo's history as provisional.
#
# N=10 is a conservative floor, NOT a measured optimum: top-5, top-20 and
# cap-off are indistinguishable from it at both budgets. The cap is kept below
# rank 10 as a guard against a pathological low-ranked symbol, but this corpus
# never exercises it — do not cite that backstop as a measured benefit.
CAP_EXEMPT_TOP_N = 10

# Cross-file direct-neighbour bypass: how many call-graph neighbours of the
# changed symbols get front-of-queue slots regardless of their blended score.
#
# A symbol with a real call edge to a changed symbol, in a DIFFERENT file, is
# the one candidate whose relevance is structural rather than inferred, and the
# blend routinely buries it under same-file and lexical noise: requests'
# `has_read` is a direct callee of `_encode_files` that ranked 26 of 247 while
# the package held 16.
#
# Two things this is NOT, both of which this module has been burned by:
#   * It is not a budget bypass. An earlier rule let any score>=80 candidate
#     skip the budget; direct callees scored ~90 after the structural bonus and
#     ate the whole budget before cheaper co-change siblings were evaluated
#     (see the header). Promoted neighbours are ordered first and pay tokens
#     like everyone else.
#   * It is not a weight change. Raising the graph weight globally lifts this
#     population too, but costs same-file recall, because the two have opposite
#     optima (benchmarks/history_signal_sweep.py). A capped structural
#     reservation buys the same neighbours without paying that.
#
# Same-file neighbours are excluded: co-location already retrieves them at
# ~80%, so spending the cap there would buy nothing.
DEFAULT_NEIGHBOUR_CAP = 5

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


def cross_file_neighbours(
    changed: List[str],
    symbols: Dict[str, Symbol],
    graph: Dict[str, List[str]],
    reverse: Dict[str, Set[str]],
) -> Set[str]:
    """Direct call-graph neighbours of `changed` that live in another file.

    Both directions count: a callee of the changed symbol may need updating,
    and a caller may break. Neither is more certain than the other, so the
    edge is treated as undirected here even though the graph is not.
    """
    changed_set = set(changed)
    changed_files = {symbols[c].file for c in changed if c in symbols}
    adjacent: Set[str] = set()
    for c in changed:
        adjacent.update(graph.get(c, ()))
        adjacent.update(reverse.get(c, ()))
    return {
        s for s in adjacent
        if s in symbols and s not in changed_set
        and symbols[s].file not in changed_files
    }


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
    neighbour_cap: int = DEFAULT_NEIGHBOUR_CAP,
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
        neighbour_cap: How many cross-file direct call-graph neighbours are
            moved to the front of the ranking regardless of score (see
            DEFAULT_NEIGHBOUR_CAP). Requires `graph`; 0 disables. Promoted
            symbols still pay the token budget and still count against
            top_k — the promotion is an ordering guarantee, not an exemption.
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
        assert reverse is not None  # derived from graph above when absent
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

    # ── Cross-file neighbour promotion ───────────────────────────────────
    # Reorder only: the highest-scored `neighbour_cap` cross-file neighbours
    # move to the head of the queue, keeping their relative order. Everything
    # downstream (top_k, per-symbol cap, budget) applies unchanged, so the
    # worst case is that the cap's worth of slots go to structurally certain
    # candidates instead of higher-scored inferred ones.
    promoted_set: Set[str] = set()
    if graph is not None and neighbour_cap > 0:
        assert reverse is not None  # derived from graph above when absent
        neighbours = cross_file_neighbours(changed, symbols, graph, reverse)
        if neighbours:
            promoted = [x for x in scored if x[0] in neighbours][:neighbour_cap]
            promoted_set = {sid for sid, _ in promoted}
            scored = promoted + [x for x in scored if x[0] not in promoted_set]

    gap_kept: Optional[Set[str]] = None
    if cutoff == "gap":
        candidates = [
            (sid, sc) for sid, sc in scored
            if sid in symbols and sc > GAP_SCORE_EPSILON
        ]
        keep_n = gap_cut_count([sc for _, sc in candidates])
        gap_kept = {sid for sid, _ in candidates[:keep_n]}

    included_non_changed = 0
    rank = 0
    for sym_id, score in scored:
        if sym_id not in symbols:
            continue
        rank += 1

        # A promoted neighbour outranks the gap heuristic: the gap reads the
        # score distribution to guess where relevance stops, and here the
        # call edge already answers that question.
        if (gap_kept is not None and sym_id not in gap_kept
                and sym_id not in promoted_set):
            dropped.append(sym_id)
            continue

        if top_k is not None and included_non_changed >= top_k:
            dropped.append(sym_id)
            continue

        sym_tokens = rendered_size(sym_id)

        # If the symbol exceeds the per-symbol cap, skip it entirely rather
        # than counting a capped amount toward the budget while emitting it
        # in full (which silently overran --max-tokens).
        #
        # The cap is deliberately NOT applied to the highest-ranked
        # candidates: it is a guard against a large irrelevant symbol, and
        # applying it score-blind made it a guard against the single most
        # relevant one. Those still have to fit the budget below.
        capped = per_sym_cap is not None and sym_tokens > per_sym_cap
        if capped and rank > CAP_EXEMPT_TOP_N:
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
