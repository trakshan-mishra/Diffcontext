"""
rerank — stage-2 precision reranking over the stage-1 hybrid candidate pool.

Stage 1 (``pipeline._blend_hybrid``) optimizes recall: it unions the graph
blast radius, the BM25 hits and every symbol in a changed file, which puts
r@100 at ~0.83 but leaves precision at 0.05-0.10 because the pool is
effectively the whole repository (see docs/RERANK.md §1).

Stage 2 rescores the top-N of that pool with a learned linear model over 22
features. Inference is **pure Python stdlib** (``json`` + ``math``) so the
shipped package keeps ``dependencies = []``; training lives benchmark-side in
``benchmarks/rerank/train.py`` and may use numpy/scipy because it runs offline.
"""

from .features import FEATURE_NAMES, QueryContext, extract_features

__all__ = ["FEATURE_NAMES", "QueryContext", "extract_features"]
