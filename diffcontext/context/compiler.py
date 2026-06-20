"""
compiler.py — Compile selected symbols into an LLM-ready context package.
"""

from typing import Dict, List

from ..models import Symbol, ContextPackage


def compile_context(
    symbols: Dict[str, Symbol],
    selected_ids: List[str],
    changed_ids: List[str],
    scores: Dict[str, float],
) -> ContextPackage:
    """
    Build the final context text from selected symbols.

    Output format:
        === CHANGED SYMBOLS ===
        FILE: path.py
        FUNCTION: name (score: 100)
        <code>

        === IMPACTED SYMBOLS ===
        ...

        === DEPENDENCIES ===
        ...
    """
    changed_set = set(changed_ids)
    total_repo_code = "\n\n".join(s.code for s in symbols.values())
    total_repo_tokens = max(1, len(total_repo_code) // 4)

    sections = {
        "CHANGED": [],
        "IMPACTED": [],
        "DEPENDENCIES": [],
    }

    for sym_id in selected_ids:
        if sym_id not in symbols:
            continue

        sym = symbols[sym_id]
        score = scores.get(sym_id, 0)
        file_name, func_name = sym_id.split(":", 1)

        entry = (
            f"FILE: {file_name}\n"
            f"FUNCTION: {func_name} (score: {score:.0f})\n\n"
            f"{sym.code}"
        )

        if sym_id in changed_set:
            sections["CHANGED"].append(entry)
        elif score >= 70:
            sections["IMPACTED"].append(entry)
        else:
            sections["DEPENDENCIES"].append(entry)

    parts = []
    for label, entries in sections.items():
        if entries:
            parts.append(f"=== {label} SYMBOLS ===\n")
            parts.append("\n\n---\n\n".join(entries))

    text = "\n\n".join(parts)
    context_tokens = max(1, len(text) // 4)

    return ContextPackage(
        text=text,
        symbol_count=len(selected_ids),
        token_estimate=context_tokens,
        total_repo_tokens=total_repo_tokens,
    )
