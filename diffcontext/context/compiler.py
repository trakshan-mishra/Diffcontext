"""
compiler.py — Compile selected symbols into an LLM-ready context package.

Key additions over the naive "dump code blocks" approach:
  - META header: LLM knows repo size, what was dropped, graph confidence,
    broken files, and scoring basis BEFORE reading any code.
  - Per-symbol relationship annotations: callers / callees, and explicit
    "NOT IN CONTEXT" tags so the LLM doesn't hallucinate missing deps.
  - Dropped-symbol manifest: explicit list of symbols cut by token budget.
  - Graph confidence score: fraction of edges that resolved to known symbols.
  - Auto-generated suggestions: rule-based hints derived from graph data.
"""

import ast
import re
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..models import Symbol, ContextPackage, ContextItem
from ..impact.scoring import describe_scoring_basis, ScoringConfig


def _get_module_docstring(abs_file_path: str) -> str:
    """
    Read the first line of the module-level docstring from an ABSOLUTE path.
    Returns "" if file is unreadable or has no docstring.
    Must receive sym.file (absolute), NOT the relative path from sym_id.
    """
    try:
        with open(abs_file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        doc = ast.get_docstring(tree)
        if doc:
            first_line = doc.strip().split("\n")[0]
            first_line = re.sub(r'^[\w/\-\.]+\.py\s*[—\-]+\s*', '', first_line).strip()
            if len(first_line) > 60:
                return first_line[:57] + "..."
            return first_line
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Per-symbol rendering — shared by compiler (emission) and selector (budgeting)
# ---------------------------------------------------------------------------

def relationship_cap(max_tokens: Optional[int]) -> int:
    """
    How many caller/callee entries a relationship block may list per symbol.
    Hub symbols carry long annotations; under a tight budget cap them harder
    so annotations can't crowd out actual code. Single source of truth so the
    selector budgets with the same cap the compiler renders with.
    """
    return 3 if (max_tokens and max_tokens < 2000) else 6


def render_symbol_block(
    sym_id: str,
    symbols: Dict[str, Symbol],
    score: float,
    graph: Optional[Dict[str, List[str]]],
    reverse: Dict[str, Set[str]],
    selected_set: Set[str],
    rel_cap: int = 6,
) -> str:
    """
    Render one symbol exactly as it appears in the compiled code section:

        FILE: {file}
        FUNCTION: {name} (score: {score})
        {CALLERS/CALLEES relationship block}
        {code}

    The selector calls this too (with a pessimistic empty selected_set, so
    every relationship entry carries the longer " [NOT IN CONTEXT]" tag) to
    budget against what will actually be emitted — budgeting on bare
    symbol.code alone undercounted headers + annotations and produced a
    systematic 25-41% budget overshoot (see CHANGELOG).
    """
    sym = symbols[sym_id]
    file_name, func_name = sym_id.split(":", 1)
    rel_block = _build_relationship_block(
        sym_id, graph, reverse, selected_set, symbols, cap=rel_cap
    )
    return (
        f"FILE: {file_name}\n"
        f"FUNCTION: {func_name} (score: {score:.0f})\n"
        + rel_block +
        f"\n{sym.code}"
    )


def build_reverse_graph(
    graph: Optional[Dict[str, List[str]]],
) -> Dict[str, Set[str]]:
    """callee -> set(callers). Shared by compiler and selector."""
    reverse: Dict[str, Set[str]] = {}
    if graph:
        for caller, callees in graph.items():
            for callee in callees:
                reverse.setdefault(callee, set()).add(caller)
    return reverse


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compile_context(
    symbols: Dict[str, Symbol],
    selected_ids: List[str],
    changed_ids: List[str],
    scores: Dict[str, float],
    graph: Optional[Dict[str, List[str]]] = None,
    dropped_ids: Optional[List[str]] = None,
    skipped_files: Optional[List[str]] = None,
    notes: Optional[str] = None,
    token_counter: Optional[Callable[[str], int]] = None,
    scoring_config: Optional[ScoringConfig] = None,
    max_tokens: Optional[int] = None,
) -> ContextPackage:
    """
    Build the final context package from selected symbols.

    The structured `items` list is the base representation; the formatted
    `text` (meta-header + sections + suggestions) is rendered from it.

    Args:
        symbols:        Full symbol table for the repo.
        selected_ids:   Symbols chosen for this context (respects token budget).
        changed_ids:    The symbols that actually changed (always selected).
        scores:         Impact scores for all scored symbols.
        graph:          Full call graph (id -> [dep ids]). Enables relationship
                        annotations and confidence calculation.
        dropped_ids:    Symbols that were scored but cut by token budget.
        skipped_files:  Files that raised SyntaxError (graph has holes here).
        notes:          Optional user notes injected into meta header.
        token_counter:  Optional text -> token count callable (real tokenizer);
                        defaults to the len//4 heuristic.
        scoring_config: The ScoringConfig used for scoring, so the meta-header
                        describes the actual run; defaults when None.
        max_tokens:     The symbol-code budget the selection ran under (None =
                        unlimited). Used to keep the meta-header proportionate:
                        under tight budgets the architecture snapshot is
                        compacted so meta can't dwarf the code it annotates.
    """
    dropped_ids   = dropped_ids   or []
    skipped_files = skipped_files or []
    count = token_counter or (lambda text: max(1, len(text) // 4))

    changed_set = set(changed_ids)

    # Build reverse graph for caller annotation
    reverse = build_reverse_graph(graph)

    # Graph confidence: fraction of edges that point to a known symbol
    graph_confidence = _compute_confidence(graph, symbols)

    # Token bookkeeping
    total_repo_code   = "\n\n".join(s.code for s in symbols.values())
    total_repo_tokens = count(total_repo_code)

    rel_cap = relationship_cap(max_tokens)

    def _assemble(sel_ids: List[str], drop_ids: List[str]):
        """Build items + full text (with the {FULL_OUTPUT_TOKENS} placeholder
        unsubstituted) for one candidate selection."""
        sel_set = set(sel_ids)

        items: List[ContextItem] = []
        for sym_id in sel_ids:
            if sym_id not in symbols:
                continue
            sym   = symbols[sym_id]
            score = scores.get(sym_id, 0)
            if sym_id in changed_set:
                role = "changed"
            elif score >= 70:
                role = "impacted"
            else:
                role = "dependency"
            items.append(ContextItem(
                symbol_id      = sym_id,
                code           = sym.code,
                score          = score,
                role           = role,
                callers        = sorted(reverse.get(sym_id, set())),
                callees        = list(graph.get(sym_id, [])) if graph else [],
                token_estimate = count(sym.code),
            ))

        sections: Dict[str, List[str]] = {"CHANGED": [], "IMPACTED": [], "DEPENDENCIES": []}
        section_of_role = {"changed": "CHANGED", "impacted": "IMPACTED", "dependency": "DEPENDENCIES"}

        for item in items:
            entry = render_symbol_block(
                item.symbol_id, symbols, item.score, graph, reverse,
                sel_set, rel_cap=rel_cap,
            )
            sections[section_of_role[item.role]].append(entry)

        parts = []
        for label, entries in sections.items():
            if entries:
                parts.append(f"=== {label} SYMBOLS ===\n")
                parts.append("\n\n---\n\n".join(entries))

        code_text = "\n\n".join(parts)
        context_tokens = count(code_text)

        meta = _build_meta_header(
            symbols        = symbols,
            selected_ids   = sel_ids,
            dropped_ids    = drop_ids,
            skipped_files  = skipped_files,
            changed_ids    = changed_ids,
            graph          = graph,
            reverse        = reverse,
            graph_confidence = graph_confidence,
            token_budget   = total_repo_tokens,   # not the budget cap; just total repo
            context_tokens = context_tokens,
            scores         = scores,
            notes          = notes,
            scoring_config = scoring_config,
            max_tokens     = max_tokens,
            count          = count,
        )

        suggestions = _build_suggestions(
            changed_ids      = changed_ids,
            dropped_ids      = drop_ids,
            skipped_files    = skipped_files,
            graph            = graph,
            reverse          = reverse,
            graph_confidence = graph_confidence,
            scores           = scores,
        )

        full_text = meta + "\n\n" + code_text
        if suggestions:
            full_text += "\n\n" + suggestions
        return items, full_text

    def _finalize_tokens(full_text: str) -> Tuple[str, int]:
        # token_estimate is the FULL output (meta + annotated code +
        # suggestions) — the number an agent harness actually pays, not just
        # the code portion. Substituting the number changes the text length,
        # so iterate to a fixed point (stabilizes after one or two rounds).
        full_tokens = count(full_text.replace("{FULL_OUTPUT_TOKENS}", "0", 1))
        candidate = full_text
        for _ in range(3):
            candidate = full_text.replace(
                "{FULL_OUTPUT_TOKENS}", f"{full_tokens:,}", 1
            )
            recount = count(candidate)
            if recount == full_tokens:
                break
            full_tokens = recount
        return candidate, full_tokens

    # --- Budget enforcement: trim AFTER rendering, against real output ---
    # The selector budgets per-symbol rendered blocks, but the meta header,
    # section separators, and suggestions are only knowable post-render.
    # Enforce max_tokens against the final full output by dropping the
    # lowest-scored non-changed symbols until it fits. Changed symbols and
    # the meta header are never dropped: the diff is the reason we're here,
    # and the meta is the disclosure layer — so when meta + changed symbols
    # alone exceed the budget, that floor is emitted as-is (and the meta's
    # own token lines report the real number, so the overshoot is visible,
    # never silent).
    selected_work = [s for s in selected_ids if s in symbols]
    dropped_work  = list(dropped_ids)

    while True:
        items, full_text = _assemble(selected_work, dropped_work)
        full_text, full_tokens = _finalize_tokens(full_text)

        if max_tokens is None or full_tokens <= max_tokens:
            break

        droppable = [s for s in selected_work if s not in changed_set]
        if not droppable:
            break  # non-compressible floor: meta + changed symbols only

        # Drop enough of the lowest-scored symbols to cover the overshoot in
        # one pass (re-checked next iteration), so the loop converges fast.
        overshoot = full_tokens - max_tokens
        droppable.sort(key=lambda s: scores.get(s, 0))
        removed, freed = [], 0
        for s in droppable:
            removed.append(s)
            freed += count(render_symbol_block(
                s, symbols, scores.get(s, 0), graph, reverse,
                set(selected_work), rel_cap=rel_cap,
            ))
            if freed >= overshoot:
                break
        removed_set = set(removed)
        selected_work = [s for s in selected_work if s not in removed_set]
        # Keep the dropped manifest ranked: trimmed symbols scored higher
        # than selection-time drops, so they go first.
        dropped_work = (
            sorted(removed, key=lambda s: -scores.get(s, 0)) + dropped_work
        )

    return ContextPackage(
        text               = full_text,
        symbol_count       = len(selected_work),
        token_estimate     = full_tokens,
        total_repo_tokens  = total_repo_tokens,
        items              = items,
        dropped_symbols    = dropped_work,
        skipped_files      = skipped_files,
        graph_confidence   = graph_confidence,
    )


# ---------------------------------------------------------------------------
# Meta-header
# ---------------------------------------------------------------------------

def _build_meta_header(
    symbols: Dict[str, Symbol],
    selected_ids: List[str],
    dropped_ids: List[str],
    skipped_files: List[str],
    changed_ids: List[str],
    graph: Optional[Dict[str, List[str]]],
    reverse: Dict[str, Set[str]],
    graph_confidence: float,
    token_budget: int,
    context_tokens: int,
    scores: Dict[str, float],
    notes: Optional[str] = None,
    scoring_config: Optional[ScoringConfig] = None,
    max_tokens: Optional[int] = None,
    count: Optional[Callable[[str], int]] = None,
) -> str:
    count = count or (lambda text: max(1, len(text) // 4))
    total_syms    = len(symbols)
    selected_cnt  = len(selected_ids)
    dropped_cnt   = len(dropped_ids)
    scored_cnt    = len(scores)
    total_edges   = sum(len(v) for v in graph.values()) if graph else 0

    direct_callers  = sum(
        1 for s in changed_ids
        for c in reverse.get(s, set())
    )
    direct_callees  = sum(
        len(graph.get(s, []))
        for s in changed_ids
    ) if graph else 0

    lines = [
        "=== DIFFCONTEXT META ===",
        f"Repo symbols total    : {total_syms}",
        f"Symbols scored        : {scored_cnt}",
        f"Symbols IN context    : {selected_cnt}",
        f"Symbols DROPPED       : {dropped_cnt}  ← you cannot see these",
        f"Graph edges total     : {total_edges}",
        f"Graph confidence      : {graph_confidence * 100:.0f}%"
          + ("  ✓" if graph_confidence >= 0.9 else "  ⚠ incomplete"),
        f"Changed symbols       : {len(changed_ids)}",
        f"Direct callers found  : {direct_callers}",
        f"Direct callees found  : {direct_callees}",
        f"Context tokens (code) : {context_tokens:,}",
        "Output tokens (full)  : {FULL_OUTPUT_TOKENS}",
        f"Scoring basis         : {describe_scoring_basis(scoring_config)}",
    ]

    # --- Repository Architecture Snapshot ---
    # Build rel_file -> absolute_path mapping from symbol table.
    # sym_id gives us relative path; sym.file gives us the absolute path we
    # need to actually open the file for its docstring.
    rel_to_abs: Dict[str, str] = {}
    for sym_id, sym in symbols.items():
        rel_file = sym_id.split(":", 1)[0]
        if rel_file not in rel_to_abs:
            rel_to_abs[rel_file] = sym.file  # sym.file is always absolute

    modules_total = {}
    modules_selected = {}
    for sym_id in symbols:
        file_name = sym_id.split(":", 1)[0]
        modules_total[file_name] = modules_total.get(file_name, 0) + 1

    for sym_id in selected_ids:
        if sym_id in symbols:
            file_name = sym_id.split(":", 1)[0]
            modules_selected[file_name] = modules_selected.get(file_name, 0) + 1

    lines.append("")
    lines.append("=== REPOSITORY ARCHITECTURE SNAPSHOT ===")

    loaded_files = []
    blind_files = []

    for file_name, total in sorted(modules_total.items()):
        selected = modules_selected.get(file_name, 0)

        doc_snippet = ""
        if file_name.endswith(".py"):
            # FIX: use absolute path, not the relative file_name
            abs_path = rel_to_abs.get(file_name, "")
            doc_str = _get_module_docstring(abs_path) if abs_path else ""
            if doc_str:
                doc_snippet = f" — {doc_str}"

        if selected > 0:
            loaded_files.append(f"  - {file_name} ({selected}/{total} symbols loaded){doc_snippet}")
        else:
            blind_files.append(f"  - {file_name} ({total} symbols){doc_snippet}")

    # Budget proportionality: the snapshot scales with repo size, not with
    # the requested budget. Under a tight budget an uncapped snapshot can
    # cost multiples of the code it annotates (measured: --max-tokens 500 on
    # black produced ~2,600 total tokens, 5x the request). Compact it when
    # it would exceed ~25% of the symbol budget.
    snapshot_cost = count("\n".join(loaded_files + blind_files))
    snapshot_budget = max(max_tokens // 4, 150) if max_tokens else None
    if snapshot_budget is not None and snapshot_cost > snapshot_budget:
        n_loaded = len(loaded_files)
        n_blind = len(blind_files)
        lines.append(
            f"MODULES: {len(modules_total)} files — {n_loaded} in context, "
            f"{n_blind} blind spots"
        )
        lines.append(
            "  (per-module snapshot omitted under tight budget — raise "
            "--max-tokens to see it)"
        )
    else:
        lines.append("MODULES IN CONTEXT:")
        if loaded_files:
            lines.extend(loaded_files)
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append("KNOWN MODULES (NOT IN CONTEXT - BLIND SPOTS):")
        if blind_files:
            _BLIND_CAP = 25
            lines.extend(blind_files[:_BLIND_CAP])
            if len(blind_files) > _BLIND_CAP:
                lines.append(f"  ... and {len(blind_files) - _BLIND_CAP} more modules")
        else:
            lines.append("  (none)")

    if skipped_files:
        lines.append("")
        lines.append(f"FILES WITH SYNTAXERROR ({len(skipped_files)}) — graph has holes here:")
        for f in skipped_files:
            lines.append(f"  ✗ {f}")

    if dropped_cnt > 0:
        # Under a tight budget, the top-15 manifest itself costs more than
        # some requested budgets — show the top 5 and keep the count honest.
        drop_cap = 5 if (max_tokens and max_tokens < 2000) else 15
        lines.append("")
        lines.append(f"DROPPED SYMBOLS ({dropped_cnt}) — scored but cut by token budget:")
        for d in dropped_ids[:drop_cap]:
            lines.append(f"  - {d}  (score: {scores.get(d, 0):.0f})")
        if dropped_cnt > drop_cap:
            lines.append(f"  ... and {dropped_cnt - drop_cap} more")
        lines.append("  → If any of these are critical, re-run with a higher --max-tokens.")

    warnings = []
    if skipped_files:
        warnings.append(
            f"⚠ {len(skipped_files)} file(s) had SyntaxErrors. "
            "Call graph may be incomplete for those files."
        )
    if dropped_cnt > 0:
        warnings.append(
            f"⚠ {dropped_cnt} symbol(s) were dropped. "
            "References to them in the code below are NOT backed by visible implementations."
        )
    if graph_confidence < 0.8:
        warnings.append(
            f"⚠ Graph confidence is {graph_confidence * 100:.0f}%. "
            "Many calls could not be resolved — likely external/stdlib deps or dynamic dispatch."
        )

    if warnings:
        lines.append("")
        for w in warnings:
            lines.append(w)

    if notes:
        lines.append(f"\n=== DEVELOPER NOTES ===\n{notes}")

    lines.append("=== END META ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-symbol relationship block
# ---------------------------------------------------------------------------

def _build_relationship_block(
    sym_id: str,
    graph: Optional[Dict[str, List[str]]],
    reverse: Dict[str, Set[str]],
    selected_set: Set[str],
    symbols: Dict[str, Symbol],
    cap: int = 6,
) -> str:
    if not graph:
        return ""

    lines = []

    callers = sorted(reverse.get(sym_id, set()))
    callees = graph.get(sym_id, [])

    if callers:
        caller_parts = []
        for c in callers[:cap]:
            tag = "" if c in selected_set else " [NOT IN CONTEXT]"
            caller_parts.append(c + tag)
        if len(callers) > cap:
            caller_parts.append(f"... +{len(callers) - cap} more")
        lines.append(f"CALLERS: {', '.join(caller_parts)}")

    if callees:
        callee_parts = []
        for c in callees[:cap]:
            tag = "" if c in selected_set else " [NOT IN CONTEXT]"
            callee_parts.append(c + tag)
        if len(callees) > cap:
            callee_parts.append(f"... +{len(callees) - cap} more")
        lines.append(f"CALLEES: {', '.join(callee_parts)}")

    if not callers and not callees:
        lines.append("CALLERS: (none found in repo)")
        lines.append("CALLEES: (none found in repo)")

    return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------------------
# Suggestions block
# ---------------------------------------------------------------------------

def _build_suggestions(
    changed_ids, dropped_ids, skipped_files,
    graph, reverse, graph_confidence, scores,
) -> str:
    tips = []

    if skipped_files:
        tips.append(
            f"Fix SyntaxErrors in {len(skipped_files)} file(s) to improve graph accuracy."
        )

    if graph_confidence < 0.7:
        tips.append(
            f"Graph confidence is {graph_confidence * 100:.0f}%. "
            "Consider running diffcontext on installed site-packages too, "
            "or add the dependency source to your repo path."
        )

    for sym_id in changed_ids:
        callers = list(reverse.get(sym_id, set()))
        if len(callers) > 20:
            tips.append(
                f"'{sym_id.split(':')[-1]}' has {len(callers)} callers — "
                "unusually high blast radius. Review changes carefully."
            )

    if dropped_ids:
        tips.append(
            f"{len(dropped_ids)} symbol(s) were dropped by token budget. "
            "Run with --max-tokens=0 (unlimited) to see full context."
        )

    if not tips:
        return ""

    lines = ["=== DIFFCONTEXT SUGGESTIONS ==="]
    for i, tip in enumerate(tips, 1):
        lines.append(f"  {i}. {tip}")
    lines.append("=== END SUGGESTIONS ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Graph confidence
# ---------------------------------------------------------------------------

def _compute_confidence(
    graph: Optional[Dict[str, List[str]]],
    symbols: Dict[str, Symbol],
) -> float:
    """
    Fraction of graph edges that resolve to a known symbol.
    Edges to external/stdlib deps count as unresolved.
    Returns 1.0 if graph is empty or None (no data = no known holes).
    """
    if not graph:
        return 1.0

    total    = 0
    resolved = 0

    for deps in graph.values():
        for d in deps:
            total += 1
            if d in symbols:
                resolved += 1

    return resolved / total if total > 0 else 1.0