"""
lexical.py — BM25 lexical retrieval signal.

Pure-stdlib BM25Okapi over symbol source code, used as the lexical leg of
hybrid retrieval. The eval_v2 benchmark showed the call graph alone loses
to full-code BM25 on most measures, while the graph+BM25+same-file blend
beats every individual signal on 4/5 repos (see benchmarks/EVAL_V2_REPORT.md);
this module is that lexical leg, with no third-party dependency.

The math replicates rank_bm25's BM25Okapi (k1=1.5, b=0.75, and negative-idf
flooring at epsilon * average_idf) so product scores match the benchmarked
implementation. Scoring uses an inverted index, so a query only touches
documents that share at least one term with it — indexing a ~9k-symbol repo
takes ~1s and a query a few milliseconds.
"""

import math
import re
from collections import Counter, defaultdict
from typing import Dict, List

from .models import Symbol

K1 = 1.5
B = 0.75
EPSILON = 0.25   # floor for negative idf, as a fraction of average idf

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def tokenize(code: str) -> List[str]:
    """Identifier tokens, lowercased, single-char tokens dropped."""
    return [t.lower() for t in _TOKEN_RE.findall(code) if len(t) > 1]


class LexicalIndex:
    """BM25 index over every symbol's source code."""

    def __init__(self, symbols: Dict[str, Symbol]):
        self.ids: List[str] = list(symbols.keys())
        n = len(self.ids)

        doc_len: List[int] = []
        term_doc_freq: Counter = Counter()          # term -> #docs containing it
        postings: Dict[str, List] = defaultdict(list)  # term -> [(doc_idx, tf)]

        for i, sid in enumerate(self.ids):
            tf = Counter(tokenize(symbols[sid].code))
            doc_len.append(sum(tf.values()))
            for term, count in tf.items():
                term_doc_freq[term] += 1
                postings[term].append((i, count))

        self.doc_len = doc_len
        self.avgdl = sum(doc_len) / n if n else 0.0
        self.postings = postings

        # idf with rank_bm25's negative-idf flooring
        idf: Dict[str, float] = {}
        idf_sum, negatives = 0.0, []
        for term, df in term_doc_freq.items():
            v = math.log(n - df + 0.5) - math.log(df + 0.5)
            idf[term] = v
            idf_sum += v
            if v < 0:
                negatives.append(term)
        if idf:
            # rank_bm25 floors negative idf at EPSILON * average_idf, but on
            # tiny corpora the average itself can be negative, which would
            # make the floor negative and silently zero out every match.
            # Clamp the floor to a small positive value instead.
            floor = EPSILON * (idf_sum / len(idf))
            if floor <= 0:
                floor = EPSILON
            for term in negatives:
                idf[term] = floor
        self.idf = idf

    def scores_for(self, query_code: str) -> Dict[str, float]:
        """
        BM25 scores of every symbol against `query_code`.

        Returns only symbols with a positive score. Duplicate query terms
        contribute once per occurrence (same as BM25Okapi.get_scores).
        """
        if not self.ids or self.avgdl == 0:
            return {}
        query_tf = Counter(tokenize(query_code))
        scores: Dict[int, float] = defaultdict(float)
        for term, q_count in query_tf.items():
            idf = self.idf.get(term)
            if idf is None:
                continue
            for doc_idx, f in self.postings[term]:
                denom = f + K1 * (1 - B + B * self.doc_len[doc_idx] / self.avgdl)
                scores[doc_idx] += q_count * idf * f * (K1 + 1) / denom
        return {self.ids[i]: s for i, s in scores.items() if s > 0}


def get_lexical_index(index) -> LexicalIndex:
    """
    Return the RepositoryIndex's lexical index, building and caching it on
    first use. pipeline.update_index() invalidates the cache when symbols
    change, so a stale index is never served.
    """
    if index._lexical is None:
        index._lexical = LexicalIndex(index.symbols)
    return index._lexical
