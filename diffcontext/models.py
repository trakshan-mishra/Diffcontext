"""
models.py — Data classes used across the pipeline.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class Symbol:
    """A single extracted symbol (function/method)."""
    id: str               # "./path.py:ClassName.method" or "./path.py:func"
    file: str             # absolute path to source file
    name: str             # bare name like "ClassName.method" or "func"
    code: str             # source code text
    lineno: int = 0       # start line number


@dataclass
class RepositoryIndex:
    """Complete index of a repository."""
    symbols: Dict[str, Symbol] = field(default_factory=dict)     # id -> Symbol
    graph: Dict[str, List[str]] = field(default_factory=dict)    # id -> [dependency ids]

    @property
    def reverse_graph(self) -> Dict[str, Set[str]]:
        """Build reverse graph (callers of each symbol)."""
        rev: Dict[str, Set[str]] = {}
        for caller, callees in self.graph.items():
            for callee in callees:
                rev.setdefault(callee, set()).add(caller)
        return rev

    @property
    def total_edges(self) -> int:
        return sum(len(deps) for deps in self.graph.values())


@dataclass
class DiffResult:
    """Result of comparing two states."""
    modified: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)

    @property
    def all_changed(self) -> List[str]:
        return self.modified + self.added


@dataclass
class ImpactResult:
    """Result of impact analysis."""
    changed: List[str] = field(default_factory=list)
    blast_radius: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)

    @property
    def all_relevant(self) -> List[str]:
        """All symbols that should be in context, deduplicated, ordered by score."""
        seen = set()
        result = []
        # Score-ordered
        scored = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)
        for sym_id, _ in scored:
            if sym_id not in seen:
                seen.add(sym_id)
                result.append(sym_id)
        # Any remaining that weren't scored
        for sym_id in self.changed + self.blast_radius + self.dependencies:
            if sym_id not in seen:
                seen.add(sym_id)
                result.append(sym_id)
        return result


@dataclass
class ContextPackage:
    """Final compiled context for an LLM."""
    text: str
    symbol_count: int
    token_estimate: int
    total_repo_tokens: int

    @property
    def reduction_pct(self) -> float:
        if self.total_repo_tokens == 0:
            return 0.0
        return (1 - self.token_estimate / self.total_repo_tokens) * 100


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""
    name: str
    repo_path: str
    changed_functions: List[str]
    # Graph stats
    total_symbols: int = 0
    total_edges: int = 0
    graph_build_ms: float = 0.0
    # Retrieval stats
    retrieved_count: int = 0
    retrieved_ids: List[str] = field(default_factory=list)
    # Token stats
    total_tokens: int = 0
    context_tokens: int = 0
    token_reduction_pct: float = 0.0
    function_reduction_pct: float = 0.0
    # Precision/Recall (when ground truth available)
    precision: Optional[float] = None
    recall: Optional[float] = None
    f1: Optional[float] = None
    # Timing
    pipeline_ms: float = 0.0
