"""
DiffContext — static-analysis-powered repository context compiler for LLMs.

Converts code changes into dependency-aware, blast-radius-aware context
packages, enabling far more accurate code understanding than keyword search
or traditional RAG.

Usage as a library:

    from diffcontext import blast_radius, index, diff, compile_context

    # Get blast radius for a symbol
    result = blast_radius("./auth.py:validate_jwt", repo="/path/to/project")
    print(result.callers)          # who calls this?
    print(result.dependencies)     # what does this call?
    print(result.total_affected)   # total transitive impact

    # Auto-detect changes and get blast radius
    result = blast_radius(ref="HEAD~1", repo="/path/to/project")

    # Index a repository
    idx = index("/path/to/project")
    print(idx.symbols)    # all parsed symbols
    print(idx.graph)      # call graph

    # Find changed symbols from git diff
    changed = diff(repo="/path/to/project", ref="HEAD~1")

    # Full context compilation for LLMs
    ctx = compile_context(ref="HEAD~1", repo="/path/to/project")
    print(ctx.text)             # LLM-ready context
    print(ctx.reduction_pct)    # how much code was filtered out
"""

__version__ = "0.2.0"

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .pipeline import index_repository, analyze_impact
from .pipeline import compile as _compile_pipeline
from .diff.git_diff import find_changed_symbols
from .impact.blast_radius import get_blast_radius


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------

@dataclass
class BlastResult:
    """Result of a blast radius analysis — the public API return type."""
    changed: List[str]
    callers: List[str]
    dependencies: List[str]
    total_affected: int
    scores: Dict[str, float] = field(default_factory=dict)
    graph: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def affected_files(self) -> List[str]:
        """Unique files in the blast radius."""
        files = set()
        for sym in self.callers:
            parts = sym.split(":", 1)
            if len(parts) == 2:
                files.add(parts[0])
        return sorted(files)

    def __repr__(self):
        return (
            f"BlastResult(changed={len(self.changed)}, "
            f"callers={len(self.callers)}, "
            f"dependencies={len(self.dependencies)}, "
            f"total_affected={self.total_affected})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def blast_radius(
    symbol: Optional[str] = None,
    *,
    ref: Optional[str] = None,
    repo: str = ".",
    depth: int = 3,
) -> BlastResult:
    """
    Compute the blast radius of a change.

    Args:
        symbol: Symbol ID like "./auth.py:validate_jwt". If None, uses `ref`.
        ref:    Git ref to auto-detect changes (e.g. "HEAD~1").
        repo:   Path to the repository root.
        depth:  Max traversal depth.

    Returns:
        BlastResult with callers, dependencies, scores, etc.

    Examples:
        >>> from diffcontext import blast_radius
        >>> r = blast_radius("./auth.py:validate_jwt", repo="/path/to/project")
        >>> r = blast_radius(ref="HEAD~1", repo="/path/to/project")
    """
    idx = index_repository(repo)

    # Determine changed symbols
    if symbol:
        changed = [symbol]
    elif ref:
        changed = find_changed_symbols(repo, idx.symbols, ref=ref)
    else:
        changed = find_changed_symbols(repo, idx.symbols, ref="HEAD~1")

    if not changed:
        return BlastResult(
            changed=[], callers=[], dependencies=[],
            total_affected=0, scores={}, graph=idx.graph,
        )

    impact = analyze_impact(idx, changed, max_depth=depth)

    return BlastResult(
        changed=impact.changed,
        callers=impact.blast_radius,
        dependencies=impact.dependencies,
        total_affected=len(impact.all_relevant),
        scores=impact.scores,
        graph=idx.graph,
    )


def index(repo: str = "."):
    """
    Index a repository: parse all Python files and build the call graph.

    Returns a RepositoryIndex with .symbols and .graph attributes.

    Example:
        >>> from diffcontext import index
        >>> idx = index("/path/to/project")
        >>> len(idx.symbols)
        354
    """
    return index_repository(repo)


def diff(repo: str = ".", ref: str = "HEAD~1") -> List[str]:
    """
    Find changed symbol IDs from git diff.

    Returns list of symbol IDs that were modified.

    Example:
        >>> from diffcontext import diff
        >>> diff(repo="/path/to/project", ref="HEAD~1")
        ['./auth.py:validate_jwt', './models.py:User.__init__']
    """
    idx = index_repository(repo)
    return find_changed_symbols(repo, idx.symbols, ref=ref)


def compile_context(
    symbol: Optional[str] = None,
    *,
    ref: Optional[str] = None,
    repo: str = ".",
    depth: int = 2,
    max_tokens: int = 10000,
):
    """
    Full pipeline: detect changes → blast radius → compile LLM context.

    Returns a ContextPackage with .text, .token_estimate, .reduction_pct.

    Example:
        >>> from diffcontext import compile_context
        >>> ctx = compile_context(ref="HEAD~1", repo="/path/to/project")
        >>> print(ctx.text)          # LLM-ready context
        >>> print(ctx.reduction_pct) # e.g. 99.2
    """
    idx = index_repository(repo)

    if symbol:
        changed = [symbol]
    elif ref:
        changed = find_changed_symbols(repo, idx.symbols, ref=ref)
    else:
        changed = find_changed_symbols(repo, idx.symbols, ref="HEAD~1")

    if not changed:
        from .models import ContextPackage
        return ContextPackage(text="", symbol_count=0, token_estimate=0, total_repo_tokens=0)

    impact = analyze_impact(idx, changed, max_depth=depth)
    return _compile_pipeline(idx, impact, max_tokens=max_tokens)
