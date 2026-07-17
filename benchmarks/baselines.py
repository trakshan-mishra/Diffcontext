#!/usr/bin/env python3
"""
baselines.py — Baseline retrieval methods for comparison.

Implements:
1. BM25 (keyword search) — the standard IR baseline
2. File-level co-location — "same file = related"
3. Random — lower bound sanity check
4. Embedding (dense retrieval) — what most RAG-for-code tooling actually
   runs in production; sentence-transformers when installed, with an
   explicitly-labeled TF-IDF-cosine fallback otherwise

These are what DiffContext must beat to prove it's actually useful.
"""

import math
import os
import re
import random
import hashlib
from collections import Counter
from typing import Dict, List, Optional, Set

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


class EmbeddingBaseline:
    """
    Dense-retrieval baseline — the comparison a developer evaluating this
    tool against modern RAG-for-code tooling actually wants.

    Two encoders, recorded in `self.encoder` so every result file states
    which one produced the numbers:

    * "sentence-transformers/<model>": real dense retrieval. Each symbol's
      full source is embedded with a small locally-runnable model (default
      all-MiniLM-L6-v2, no paid API); retrieval is cosine over normalized
      vectors. Used when sentence-transformers is importable.
    * "tfidf-cosine-approx": pure-Python TF-IDF cosine over the same
      identifier tokenization BM25 uses. This is NOT dense retrieval — it
      is a lexical-vector approximation, kept only so the benchmark runs
      in environments where installing torch is undesirable (e.g. CI).
      Results produced with this encoder must not be presented as an
      embedding comparison; the eval harness labels them.
    """

    ST_MODEL = "all-MiniLM-L6-v2"

    def __init__(self, symbols: Dict[str, Symbol], prefer_dense: bool = True):
        self.symbol_ids = list(symbols.keys())
        self.symbols = symbols
        self._st_model = None
        self._matrix = None          # dense path: (n_symbols, dim) normalized
        self._tfidf_vecs = None      # fallback path: list of {token: weight}
        self._idf: Optional[Dict[str, float]] = None

        if prefer_dense:
            try:
                from sentence_transformers import SentenceTransformer
                self._st_model = SentenceTransformer(self.ST_MODEL)
                self.encoder = f"sentence-transformers/{self.ST_MODEL}"
            except Exception as e:  # noqa: BLE001 — ImportError, or model
                # download failure (offline / network-policy environments).
                # Fall back rather than abort the whole benchmark run; the
                # encoder label makes the substitution visible in results.
                print(f"  [embedding baseline] dense encoder unavailable "
                      f"({type(e).__name__}); using tfidf-cosine-approx")
                self._st_model = None

        if self._st_model is not None:
            import numpy as np
            texts = [symbols[sid].code for sid in self.symbol_ids]
            emb = self._st_model.encode(
                texts, batch_size=64, show_progress_bar=False,
                convert_to_numpy=True, normalize_embeddings=True,
            )
            self._matrix = np.asarray(emb)
        else:
            self.encoder = "tfidf-cosine-approx"
            n = len(self.symbol_ids)
            df: Counter = Counter()
            token_lists = []
            for sid in self.symbol_ids:
                toks = _tokenize(symbols[sid].code)
                token_lists.append(toks)
                df.update(set(toks))
            self._idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
            self._tfidf_vecs = []
            for toks in token_lists:
                tf = Counter(toks)
                vec = {t: (1 + math.log(c)) * self._idf[t] for t, c in tf.items()}
                norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
                self._tfidf_vecs.append({t: w / norm for t, w in vec.items()})
        self._id_to_idx = {sid: i for i, sid in enumerate(self.symbol_ids)}

    def retrieve(self, query_symbol_id: str, top_k: int = 30) -> List[str]:
        """Top-k most similar symbols to the query symbol's source."""
        qi = self._id_to_idx.get(query_symbol_id)
        if qi is None:
            return []

        if self._matrix is not None:
            sims = self._matrix @ self._matrix[qi]
            order = sims.argsort()[::-1]
            out = []
            for i in order:
                sid = self.symbol_ids[int(i)]
                if sid != query_symbol_id:
                    out.append(sid)
                if len(out) >= top_k:
                    break
            return out

        qvec = self._tfidf_vecs[qi]
        scored = []
        for i, vec in enumerate(self._tfidf_vecs):
            if i == qi:
                continue
            small, large = (qvec, vec) if len(qvec) < len(vec) else (vec, qvec)
            sim = sum(w * large.get(t, 0.0) for t, w in small.items())
            if sim > 0:
                scored.append((self.symbol_ids[i], sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [sid for sid, _ in scored[:top_k]]


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
