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
    broken_files: List[str] = field(default_factory=list)

    # Incremental-update state, populated by pipeline.index_repository().
    # Private: not part of the public API, excluded from repr/comparison.
    _repo_path: Optional[str] = field(default=None, repr=False, compare=False)
    _file_trees: Optional[Dict] = field(default=None, repr=False, compare=False)
    _import_maps: Optional[Dict] = field(default=None, repr=False, compare=False)
    _warn_state: Optional[object] = field(default=None, repr=False, compare=False)
    # Lazily built BM25 index (see lexical.get_lexical_index); invalidated
    # by update_index() whenever symbols change.
    _lexical: Optional[object] = field(default=None, repr=False, compare=False)
    # Lazily built reverse call graph; invalidated by update_index()
    # whenever the forward graph is rebuilt.
    _reverse_graph: Optional[Dict[str, Set[str]]] = field(default=None, repr=False, compare=False)

    def update(self, changed_files: List[str]) -> "RepositoryIndex":
        """
        Incrementally re-index after `changed_files` were edited, created,
        or deleted. Only those files are re-read and re-parsed; the graph
        is rebuilt from in-memory ASTs. Mutates and returns this index.

        Only available on indexes created by pipeline.index_repository().
        """
        from .pipeline import update_index
        return update_index(self, changed_files)

    @property
    def reverse_graph(self) -> Dict[str, Set[str]]:
        """
        Reverse graph (callers of each symbol), computed once per index
        state and cached. Treat the returned dict as read-only: it is
        shared across callers and only invalidated by update().
        """
        if self._reverse_graph is None:
            rev: Dict[str, Set[str]] = {}
            for caller, callees in self.graph.items():
                for callee in callees:
                    rev.setdefault(callee, set()).add(caller)
            self._reverse_graph = rev
        return self._reverse_graph

    @property
    def total_edges(self) -> int:
        return sum(len(deps) for deps in self.graph.values())


@dataclass
class DiffResult:
    """Result of comparing two states."""
    modified: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    broken_files: List[str] = field(default_factory=list)

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
class ContextItem:
    """
    One selected symbol, in structured form. The base representation of a
    compiled context: a harness can filter, reorder, and re-budget these
    itself instead of consuming the pre-rendered text.
    """
    symbol_id: str                 # "./path.py:ClassName.method"
    code: str                      # full source of the symbol
    score: float                   # impact score (higher = more relevant)
    role: str                      # "changed" | "impacted" | "dependency"
    callers: List[str] = field(default_factory=list)   # full list, untruncated
    callees: List[str] = field(default_factory=list)   # full list, untruncated
    token_estimate: int = 0


@dataclass
class ContextPackage:
    """
    Final compiled context for an LLM.

    `items` is the structured base representation; `text` is one renderer
    over it (meta-header + sections + suggestions) for direct LLM pasting.
    """
    text: str
    symbol_count: int
    token_estimate: int
    total_repo_tokens: int
    # Structured selection — the machine-consumable form of `text`'s body.
    items: List[ContextItem] = field(default_factory=list)
    # LLM self-awareness fields
    dropped_symbols: List[str] = field(default_factory=list)   # scored but cut by budget
    skipped_files: List[str] = field(default_factory=list)     # SyntaxError'd files
    graph_confidence: float = 1.0                              # fraction of edges that resolved

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