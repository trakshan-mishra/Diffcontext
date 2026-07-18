"""
languages/ — Optional per-language adapters beyond the built-in Python
support.

The core pipeline (scoring, selection, compilation, caching, diff mapping)
is language-agnostic: it operates on `Symbol` records and a symbol-id
graph. What a language needs to supply is exactly what parser.py,
resolver.py, and graph_builder.py supply for Python:

    1. symbols per file  (id "./rel/path.ext:Name", code, lineno)
    2. a dependency graph over those symbol ids

An adapter provides both. Adapters have runtime dependencies (tree-sitter
grammars) that the core deliberately does not: they are OPTIONAL extras,
probed at import time — without them installed, DiffContext behaves
exactly as the Python-only tool it was, no warnings, no degradation of
the Python path.

Honesty contract: adapter-produced graphs are shallower than the Python
graph (no attribute-type tracking, no cross-file MRO). Retrieval quality
for adapter languages is NOT covered by the benchmark numbers in the
README until measured separately — see the language support table there.
"""

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_adapters: "Optional[List]" = None


def available_adapters() -> "List":
    """
    Adapters whose runtime dependencies are importable, probed once per
    process. An adapter that fails to import (missing extra, grammar ABI
    mismatch) is skipped silently at INFO level — the Python path must
    never degrade because an optional extra is absent or broken.
    """
    global _adapters
    if _adapters is None:
        _adapters = []
        try:
            from .typescript import TypeScriptAdapter
            _adapters.append(TypeScriptAdapter())
        except Exception as e:  # ImportError, or grammar version mismatch
            logger.info("TypeScript adapter unavailable: %s", e)
    return _adapters


def discover_files(adapter, repo_path: str) -> "List[str]":
    """All files this adapter should index in repo_path: extension match
    (gitignore-aware) filtered through the adapter's own indexing policy
    (vendored/minified/test-file exclusions)."""
    from ..scanner import find_source_files
    return [
        f for f in find_source_files(repo_path, tuple(adapter.extensions))
        if adapter.should_index(f)
    ]


def adapter_for_path(path: str):
    """The adapter that handles this file's extension, or None."""
    for adapter in available_adapters():
        if path.endswith(adapter.extensions):
            return adapter
    return None


def indexable_extensions() -> "Tuple[str, ...]":
    """
    Every file extension the current environment can index: Python always,
    plus each available adapter's extensions. Used by file discovery and
    git-diff filtering so a changed file is only ever reported when its
    symbols can actually exist in the index.
    """
    exts: "Tuple[str, ...]" = (".py",)
    for adapter in available_adapters():
        exts = exts + tuple(adapter.extensions)
    return exts
