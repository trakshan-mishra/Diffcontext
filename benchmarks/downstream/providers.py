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
  bm25             rank-BM25 over full function sources (strongest single
                   baseline per RIGOR_REPORT §5)
  samefile         same-file co-location
  none             empty context (floor; also the memorization probe — see
                   README: if `none` solves tasks, the model knows the fix
                   from pretraining and absolute pass rates are inflated,
                   though paired deltas between arms remain interpretable)
"""

from typing import Callable, Dict, List, Tuple

from diffcontext.models import RepositoryIndex
from diffcontext.pipeline import analyze_impact
from diffcontext.context.selector import GAP_SCORE_EPSILON, gap_cut_count

PROVIDERS = ["diffcontext", "diffcontext_gap", "bm25", "samefile", "none"]


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4 * 1.2))  # selector.py heuristic, kept identical


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


def rank_none(index: RepositoryIndex, seeds: List[str]) -> List[str]:
    return []


RANKERS: Dict[str, Callable[[RepositoryIndex, List[str]], List[str]]] = {
    "diffcontext": rank_diffcontext,
    "diffcontext_gap": rank_diffcontext_gap,
    "bm25": rank_bm25,
    "samefile": rank_samefile,
    "none": rank_none,
}


def render_context(index: RepositoryIndex, ranked: List[str], max_tokens: int) -> str:
    """Pack ranked symbols into the budget. Identical rendering for every
    provider — headers + source, nothing provider-specific."""
    parts: List[str] = []
    used = 0
    for sid in ranked:
        sym = index.symbols.get(sid)
        if sym is None:
            continue
        block = f"# {sid}\n{sym.code}\n"
        cost = _estimate_tokens(block)
        if used + cost > max_tokens:
            continue
        parts.append(block)
        used += cost
    return "\n".join(parts)


def compile_provider_context(
    index: RepositoryIndex, provider: str, seeds: List[str], max_tokens: int,
) -> str:
    seeds_in_index = [s for s in seeds if s in index.symbols]
    if not seeds_in_index:
        return ""
    ranked = RANKERS[provider](index, seeds_in_index)
    return render_context(index, ranked, max_tokens)
