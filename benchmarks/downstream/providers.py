"""
providers.py — context providers for the downstream eval.

Every provider answers the same question under the same constraints:
given the task's seed symbols (oracle localization — the functions the
gold patch modifies, as they exist BEFORE the fix), return a ranked list
of OTHER symbols worth showing the model. A shared renderer then packs
each ranking into the same token budget, so the ONLY thing that differs
between arms is which code fills the window — same model, same prompt,
same budget, same seeds.

Providers:
  diffcontext      hybrid retrieval, recall-first top-k (the product default)
  diffcontext_gap  hybrid retrieval + the largest-gap precision cutoff
  semantic         dense retrieval over function sources (sentence-transformers);
                   what most RAG-for-code tooling actually runs. Falls back to a
                   lexical TF-IDF approximation when the encoder is unavailable —
                   `semantic_encoder()` reports which one ran, and the eval layer
                   records it on every row so a fallback can never be read as a
                   dense-retrieval result
  bm25             rank-BM25 over full function sources (strongest single
                   baseline per RIGOR_REPORT §5)
  samefile         same-file co-location
  fullrepo         no retrieval at all — every symbol in the repo, in source
                   order, packed until the budget runs out. This is the
                   "just paste the codebase" arm, and it is only meaningful
                   when given its own (much larger) budget: see FULLREPO_TOKENS
                   and `ContextResult.truncated`
  none             empty context (floor; also the memorization probe — see
                   README: if `none` solves tasks, the model knows the fix
                   from pretraining and absolute pass rates are inflated,
                   though paired deltas between arms remain interpretable)
"""

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from diffcontext.models import RepositoryIndex
from diffcontext.pipeline import analyze_impact
from diffcontext.context.selector import GAP_SCORE_EPSILON, gap_cut_count

# Every arm the harness knows how to run.
PROVIDERS = ["diffcontext", "diffcontext_gap", "semantic", "bm25", "samefile",
             "fullrepo", "none"]
# What run_eval.py measures unless told otherwise. Deliberately NOT all of
# PROVIDERS: `fullrepo` and `semantic` were added later, and rolling them into
# the default would silently change the cost and the meaning of every existing
# results file. Opt in with --providers.
DEFAULT_PROVIDERS = ["diffcontext", "diffcontext_gap", "bm25", "samefile", "none"]


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4 * 1.2))  # selector.py heuristic, kept identical


def _estimate_tokens_from_chars(n_chars: int) -> int:
    """_estimate_tokens() over a string of n_chars, without building it."""
    return max(1, int(n_chars / 4 * 1.2))


def _hybrid_ranking(index: RepositoryIndex, seeds: List[str]) -> List[Tuple[str, float]]:
    impact = analyze_impact(index, seeds)
    seed_set = set(seeds)
    ranked = sorted(
        ((sid, sc) for sid, sc in impact.scores.items()
         if sid not in seed_set and sid in index.symbols),
        key=lambda x: x[1], reverse=True,
    )
    return ranked  # (sid, score) pairs; callers strip or cut


def rank_diffcontext(index: RepositoryIndex, seeds: List[str]) -> List[str]:
    return [sid for sid, _ in _hybrid_ranking(index, seeds)]


def rank_diffcontext_gap(index: RepositoryIndex, seeds: List[str]) -> List[str]:
    ranked = [(sid, sc) for sid, sc in _hybrid_ranking(index, seeds)
              if sc > GAP_SCORE_EPSILON]
    keep = gap_cut_count([sc for _, sc in ranked])
    return [sid for sid, _ in ranked[:keep]]


def rank_bm25(index: RepositoryIndex, seeds: List[str]) -> List[str]:
    from benchmarks.baselines import BM25Baseline
    bl = BM25Baseline(index.symbols)
    seen: Dict[str, float] = {}
    for seed in seeds:
        for rank, sid in enumerate(bl.retrieve(seed, top_k=100)):
            score = 1.0 / (rank + 1)
            if score > seen.get(sid, 0.0):
                seen[sid] = score
    seed_set = set(seeds)
    return [sid for sid, _ in sorted(seen.items(), key=lambda x: -x[1])
            if sid not in seed_set]


def rank_samefile(index: RepositoryIndex, seeds: List[str]) -> List[str]:
    from benchmarks.baselines import FileCoLocationBaseline
    bl = FileCoLocationBaseline(index.symbols)
    out, seen = [], set(seeds)
    for seed in seeds:
        for sid in bl.retrieve(seed, top_k=100):
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


def rank_fullrepo(index: RepositoryIndex, seeds: List[str]) -> List[str]:
    """No retrieval: every symbol in the repository, in source order.

    Source order (file path, then line number) is the order a human pasting
    the codebase would produce, and it is deterministic — so this arm measures
    "the whole repo, unranked", not "an arbitrary shuffle". Whether it fits is
    the point: the caller gives this arm its own budget (FULLREPO_TOKENS) and
    reads `ContextResult.truncated` to see whether the repo actually fit.
    """
    seed_set = set(seeds)
    return sorted(
        (sid for sid in index.symbols if sid not in seed_set),
        key=lambda sid: (index.symbols[sid].file, index.symbols[sid].lineno, sid),
    )


# The dense encoder is expensive to construct (model load) and to run (one
# forward pass per symbol). Both are cached for the lifetime of the process:
# the model once, and each symbol's vector by a hash of its SOURCE, so a sweep
# that re-indexes one repo at N successive commits pays for each distinct
# function body once rather than N times.
_ST_MODEL: List[object] = []          # 0-or-1 element: a loaded-model slot
_EMBED_CACHE: Dict[str, object] = {}
_ENCODER_LABEL: List[str] = []


def _load_st_model() -> Optional[object]:
    if _ST_MODEL:
        return _ST_MODEL[0]
    try:
        from sentence_transformers import SentenceTransformer
        from benchmarks.baselines import EmbeddingBaseline
        _ST_MODEL.append(SentenceTransformer(EmbeddingBaseline.ST_MODEL))
    except Exception as e:  # noqa: BLE001 — missing package or model download
        print(f"  [semantic] dense encoder unavailable ({type(e).__name__}: {e}); "
              f"the semantic arm will run its LEXICAL fallback")
        return None
    return _ST_MODEL[0]


def semantic_encoder() -> str:
    """Which encoder the semantic arm actually ran, e.g.
    'sentence-transformers/all-MiniLM-L6-v2' or 'tfidf-cosine-approx'.
    Empty until the arm has been built once. Recorded on every result row:
    a lexical fallback must never be reported as dense retrieval."""
    return _ENCODER_LABEL[0] if _ENCODER_LABEL else ""


# Corpus-build cost of the most recent semantic ranking, in ms. Embedding the
# corpus is an INDEXING cost — paid once per corpus and amortized over every
# query against it — and pooling it into per-query retrieval latency is how a
# dense baseline ends up looking 260x slower than it is. compile_context()
# lifts it out into its own field so the two are never summed by accident.
_LAST_CORPUS_MS: List[float] = [0.0]


def warm_semantic(index: RepositoryIndex) -> float:
    """Load the encoder and embed `index` up front; return the ms it cost.

    Without this the first measured query on a process pays model load plus a
    full-corpus forward pass and reports it as retrieval time. Callers that
    measure latency should call this before the first timed arm — and should
    record the returned cost rather than discard it: warming makes the number
    honest by moving it to the right column, not by deleting it.
    """
    if "semantic" not in RANKERS:
        return 0.0
    t0 = time.perf_counter()
    rank_semantic(index, list(index.symbols)[:1])
    return (time.perf_counter() - t0) * 1000


def semantic_cache_stats() -> Dict[str, int]:
    """Vectors held by the process-wide embedding cache. A sweep over N commits
    of one repo re-embeds only what actually changed, so this is far below
    N x len(symbols) — which is the point of the cache, and worth reporting so
    the dense arm's amortized cost is not mistaken for its cold cost."""
    return {"cached_vectors": len(_EMBED_CACHE)}


def rank_semantic(index: RepositoryIndex, seeds: List[str]) -> List[str]:
    """Dense retrieval — fused across seeds by reciprocal rank, exactly as the
    bm25 arm fuses, so the two differ only in the similarity function."""
    from benchmarks.baselines import EmbeddingBaseline
    t0 = time.perf_counter()
    bl = EmbeddingBaseline(index.symbols, cache=_EMBED_CACHE, model=_load_st_model())
    _LAST_CORPUS_MS[0] = (time.perf_counter() - t0) * 1000
    if not _ENCODER_LABEL:
        _ENCODER_LABEL.append(bl.encoder)
    seen: Dict[str, float] = {}
    for seed in seeds:
        for rank, sid in enumerate(bl.retrieve(seed, top_k=100)):
            score = 1.0 / (rank + 1)
            if score > seen.get(sid, 0.0):
                seen[sid] = score
    seed_set = set(seeds)
    return [sid for sid, _ in sorted(seen.items(), key=lambda x: -x[1])
            if sid not in seed_set]


def rank_none(index: RepositoryIndex, seeds: List[str]) -> List[str]:
    return []


RANKERS: Dict[str, Callable[[RepositoryIndex, List[str]], List[str]]] = {
    "diffcontext": rank_diffcontext,
    "diffcontext_gap": rank_diffcontext_gap,
    "semantic": rank_semantic,
    "bm25": rank_bm25,
    "samefile": rank_samefile,
    "fullrepo": rank_fullrepo,
    "none": rank_none,
}


def render_context(index: RepositoryIndex, ranked: List[str],
                   max_tokens: int) -> str:
    """Pack ranked symbols into the budget. Identical rendering for every
    provider — headers + source, nothing provider-specific."""
    return _render(index, ranked, max_tokens)[0]


def _render(index: RepositoryIndex, ranked: List[str],
            max_tokens: int) -> Tuple[str, List[str], int]:
    """(text, ids actually included, symbols skipped for want of budget).

    Returning the included ids is what lets the eval layer score precision and
    recall over the context the model REALLY saw, rather than over the ranked
    list the provider proposed — the two differ whenever the budget bites,
    which for the fullrepo arm is essentially always.
    """
    parts: List[str] = []
    included: List[str] = []
    used_chars = 0
    dropped = 0
    for sid in ranked:
        sym = index.symbols.get(sid)
        if sym is None:
            continue
        block = f"# {sid}\n{sym.code}\n"
        # Budget against the characters the final "\n".join() will actually
        # emit — including the separator this block brings with it. Summing
        # per-block _estimate_tokens() instead undercounts twice: it misses
        # the join separators, and it floors once per block rather than once
        # over the whole text, so the assembled string can exceed max_tokens.
        cand_chars = used_chars + len(block) + (1 if parts else 0)
        if _estimate_tokens_from_chars(cand_chars) > max_tokens:
            dropped += 1
            continue
        parts.append(block)
        included.append(sid)
        used_chars = cand_chars
    return "\n".join(parts), included, dropped


@dataclass
class ContextResult:
    """One arm's context for one task, with everything needed to score it."""
    provider: str
    text: str
    included: List[str] = field(default_factory=list)   # ids in the prompt
    ranked_n: int = 0             # candidates the provider proposed
    dropped_n: int = 0            # proposed but cut for want of budget
    context_tokens: int = 0       # estimated tokens of `text`
    budget: int = 0               # the budget this arm was given
    retrieval_ms: float = 0.0     # per-query ranking, steady state
    corpus_build_ms: float = 0.0  # one-time corpus prep (dense embedding)
    render_ms: float = 0.0        # packing only

    @property
    def truncated(self) -> bool:
        """Did the budget cut off candidates this arm wanted to show?"""
        return self.dropped_n > 0

    @property
    def latency_ms(self) -> float:
        return self.retrieval_ms + self.render_ms


def compile_context(index: RepositoryIndex, provider: str, seeds: List[str],
                    max_tokens: int) -> ContextResult:
    """Compile one arm's context and measure it.

    Latency is split: `retrieval_ms` is the arm's own ranking work — the number
    that belongs in a retrieval-latency comparison — while `render_ms` is the
    budget packing, which is shared, identical code for every arm.
    """
    res = ContextResult(provider=provider, text="", budget=max_tokens)
    seeds_in_index = [s for s in seeds if s in index.symbols]
    if not seeds_in_index:
        return res

    _LAST_CORPUS_MS[0] = 0.0
    t0 = time.perf_counter()
    ranked = RANKERS[provider](index, seeds_in_index)
    elapsed = (time.perf_counter() - t0) * 1000
    # Corpus prep is amortized, not per-query: charge it to its own field so a
    # cold first call cannot masquerade as this arm's query latency.
    res.corpus_build_ms = _LAST_CORPUS_MS[0]
    res.retrieval_ms = max(0.0, elapsed - res.corpus_build_ms)
    res.ranked_n = len(ranked)

    t0 = time.perf_counter()
    res.text, res.included, res.dropped_n = _render(index, ranked, max_tokens)
    res.render_ms = (time.perf_counter() - t0) * 1000
    res.context_tokens = _estimate_tokens(res.text) if res.text else 0
    return res


def compile_provider_context(
    index: RepositoryIndex, provider: str, seeds: List[str], max_tokens: int,
) -> str:
    """Back-compat wrapper: just the prompt text (used by run_eval.py)."""
    return compile_context(index, provider, seeds, max_tokens).text
