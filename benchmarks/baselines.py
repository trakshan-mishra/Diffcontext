#!/usr/bin/env python3
"""
baselines.py — Baseline retrieval methods for comparison.

Implements:
1. BM25 (keyword search) — the standard IR baseline
2. File-level co-location — "same file = related"
3. Random — lower bound sanity check

These are what DiffContext must beat to prove it's actually useful.
"""

import os
import re
import random
import hashlib
from typing import Dict, List, Set

from rank_bm25 import BM25Okapi

from diffcontext.models import Symbol


def _tokenize(code: str) -> List[str]:
    """Simple tokenizer: split on non-alphanumeric, lowercase, filter short."""
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', code)
    return [t.lower() for t in tokens if len(t) > 1]


class BM25Baseline:
    """
    BM25 retrieval baseline.

    Given a query function's code, find the most similar functions
    by keyword overlap. This is what most RAG systems approximate.
    """

    def __init__(self, symbols: Dict[str, Symbol]):
        self.symbol_ids = list(symbols.keys())
        self.symbols = symbols

        # Build BM25 index
        corpus = [_tokenize(symbols[sid].code) for sid in self.symbol_ids]
        self.bm25 = BM25Okapi(corpus)

    def retrieve(
        self,
        query_symbol_id: str,
        top_k: int = 30,
    ) -> List[str]:
        """
        Given a changed function, retrieve the top-k most similar
        functions by BM25 keyword matching.
        """
        if query_symbol_id not in self.symbols:
            return []

        query_tokens = _tokenize(self.symbols[query_symbol_id].code)
        scores = self.bm25.get_scores(query_tokens)

        # Rank by score
        ranked = sorted(
            zip(self.symbol_ids, scores),
            key=lambda x: x[1],
            reverse=True,
        )

        # Return top-k, excluding the query itself
        return [
            sid for sid, score in ranked[:top_k + 1]
            if sid != query_symbol_id and score > 0
        ][:top_k]


class FileCoLocationBaseline:
    """
    File co-location baseline.

    "If the query function is in file X, all other functions in file X
    are relevant." This is the simplest possible heuristic.
    """

    def __init__(self, symbols: Dict[str, Symbol]):
        self.symbols = symbols
        # Group by file
        self.file_groups: Dict[str, List[str]] = {}
        for sid, sym in symbols.items():
            rel_file = sid.split(":")[0]
            self.file_groups.setdefault(rel_file, []).append(sid)

    def retrieve(self, query_symbol_id: str, top_k: int = 30) -> List[str]:
        """Return all functions in the same file as the query."""
        rel_file = query_symbol_id.split(":")[0]
        same_file = [
            sid for sid in self.file_groups.get(rel_file, [])
            if sid != query_symbol_id
        ]
        return same_file[:top_k]


class RandomBaseline:
    """Random baseline — lower bound sanity check."""

    def __init__(self, symbols: Dict[str, Symbol], seed: int = 42):
        self.symbol_ids = sorted(symbols.keys())
        self.seed = seed

    def retrieve(self, query_symbol_id: str, top_k: int = 30) -> List[str]:
        """Return a deterministic random top-k for fair, repeatable evals."""
        candidates = [s for s in self.symbol_ids if s != query_symbol_id]
        seed_bytes = f"{self.seed}:{query_symbol_id}".encode("utf-8")
        case_seed = int(hashlib.sha256(seed_bytes).hexdigest()[:16], 16)
        rng = random.Random(case_seed)
        return rng.sample(candidates, min(top_k, len(candidates)))
