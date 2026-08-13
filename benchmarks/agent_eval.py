#!/usr/bin/env python3
"""
agent_eval.py — the agent evaluation layer: one command, one task unit, all
seven metrics, three arms.

Everything else in benchmarks/ measures ONE thing well and reports it in its
own units: context_reduction.py sizes the prompt, eval_v2_hardened.py scores
retrieval against co-change ground truth, downstream/run_eval.py runs the LLM
and lets the repo's tests judge. A reader who wants the question this project
actually exists to answer — "is retrieved context worth it, and at what cost?"
— has to join three result files measured on three different units.

This joins them. The unit is ONE MINED TASK (downstream/tasks.py: a real
commit whose own tests fail before the fix and pass after), because that is
the only unit that can carry an agent-success number at all. For each task and
each arm it records:

  full_repo_tokens        cost of pasting the entire repository
  context_tokens          cost of what the arm actually put in the prompt
  token_reduction_pct     1 - context/full_repo
  context_precision       of what we showed, how much was needed
  context_recall          of what was needed, how much we showed
  agent_success           did the LLM's patch make the repo's tests pass
  latency_ms              retrieval, packing, and generation, separately

Arms, by default the three-way comparison worth arguing about:

  fullrepo      no retrieval — paste the codebase in source order
  semantic      dense retrieval (sentence-transformers), i.e. ordinary RAG
  diffcontext   the dependency-graph hybrid

---------------------------------------------------------------------------
How precision and recall are defined here — read this before quoting them
---------------------------------------------------------------------------
These tasks use ORACLE LOCALIZATION: every arm is seeded with the functions
the gold patch modifies. That is deliberate (it isolates context quality from
localization ability) but it means the obvious relevance set — "the symbols
the fix touched" — has already been handed to the model. Scored directly,
recall would be undefined and precision would be zero for every arm, which
measures nothing.

So retrieval quality is scored under LEAVE-ONE-OUT seeding: for a task whose
gold patch touched symbols S (|S| >= 2), each s in S is held out in turn, the
arm is seeded with S \\ {s}, and the relevant set is {s} — "given the rest of
this change, does the arm surface the remaining piece?" Results are averaged
over the folds. Tasks with |S| == 1 have no held-out symbol and are reported
as `loo_eligible: false` with null precision/recall — never as zero.

The consequence to state whenever these numbers appear: the agent-success
pass runs on FULL oracle seeding (unchanged from run_eval.py), while
precision/recall come from the leave-one-out pass. They describe the same arm
on the same task, but not the same prompt. Precision/recall characterize the
arm's retrieval; agent_success characterizes the arm's context. Do not read
the former as a description of the prompt that produced the latter.

`context_precision` is |relevant & shown| / |shown|, over what survived the
budget — not over the ranked list. An arm that proposes a perfect ranking and
then overflows the window gets no credit for the part the model never saw.

Token counts use the renderer's own estimator (len/4 * 1.2) applied to the
rendered block format, so `full_repo_tokens` is literally "this repo, in the
same format the arms emit, with no budget" and every reduction is a ratio of
two like-for-like measurements. They are estimates, not tiktoken output;
the ratio is what is being claimed, not the absolute counts.

---------------------------------------------------------------------------
Usage
---------------------------------------------------------------------------
  # Retrieval + token + latency metrics. No API key, no cost.
  python benchmarks/agent_eval.py --tasks benchmarks/downstream/tasks/click.json \\
      --repo benchmark_repos/click

  # Add agent success rate. --mock is the free self-test; --backend spends API.
  python benchmarks/agent_eval.py --tasks ... --repo ... --mock gold
  python benchmarks/agent_eval.py --tasks ... --repo ... --backend gemini --samples 3

  # The table.
  python benchmarks/agent_eval.py --report benchmarks/results/agent_eval/click.jsonl
"""

import argparse
import json
import os
import shutil
import statistics
import sys
import time
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.downstream.providers import (
    PROVIDERS, compile_context, semantic_encoder, warm_semantic,
)
from benchmarks.downstream.run_eval import (
    DEFAULT_MODELS, OPENAI_COMPAT, apply_and_test, generate_patch,
    is_transient_error,
)
from benchmarks.downstream.tasks import Task, Worktree, _git
from benchmarks.significance import holm_bonferroni, wilcoxon_signed_rank
from diffcontext.pipeline import index_repository

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "results", "agent_eval")

# The comparison the eval exists to make: no retrieval, ordinary RAG, this project.
DEFAULT_ARMS = ["fullrepo", "semantic", "diffcontext"]
DEFAULT_CONTEXT_TOKENS = 8000
# What the fullrepo arm is allowed to spend. Deliberately NOT the same as
# --context-tokens: capping "paste the whole repo" at a retrieval-sized budget
# would reduce it to "the alphabetically-first 8k tokens", which is a strawman
# rather than a baseline. This default is a large modern context window; when
# the repo still doesn't fit, that IS the result, and the report says so.
DEFAULT_FULLREPO_TOKENS = 128000
# Budget for measuring the un-capped repo. Not a real prompt — just a ceiling
# high enough that the renderer never truncates, so the measurement is "the
# whole repo" and not "the budget".
UNLIMITED = 10 ** 9


# ---------------------------------------------------------------------------
# Per-task measurement
# ---------------------------------------------------------------------------

def _mean(vals: Sequence[float]) -> Optional[float]:
    return statistics.mean(vals) if vals else None


def measure_retrieval(index, arm: str, task: Task, seeds: List[str],
                      budget: int) -> Dict:
    """Leave-one-out retrieval quality for one arm on one task.

    Returns nulls (not zeros) when the task has fewer than two seeds: with
    nothing to hold out there is no question to ask, and scoring it zero would
    silently drag every arm's mean toward the floor in proportion to how many
    single-symbol commits the repo happens to have.
    """
    if len(seeds) < 2:
        return {"loo_eligible": False, "loo_folds": 0, "context_precision": None,
                "context_recall": None, "gold_rank": None}

    precisions: List[float] = []
    recalls: List[float] = []
    ranks: List[Optional[int]] = []
    for held_out in seeds:
        rest = [s for s in seeds if s != held_out]
        res = compile_context(index, arm, rest, budget)
        shown = set(res.included)
        hit = held_out in shown
        precisions.append((1.0 / len(shown)) if (hit and shown) else 0.0)
        recalls.append(1.0 if hit else 0.0)
        ranks.append(res.included.index(held_out) + 1 if hit else None)

    found = [r for r in ranks if r is not None]
    return {
        "loo_eligible": True,
        "loo_folds": len(seeds),
        "context_precision": _mean(precisions),
        "context_recall": _mean(recalls),
        # Mean rank AMONG FOLDS WHERE IT WAS FOUND. Null when never found —
        # averaging in a sentinel for the misses would make a shallow arm that
        # finds one symbol at rank 1 beat a deep arm that finds four at rank 8.
        "gold_rank": _mean(found) if found else None,
    }


def measure_task(index, task: Task, seeds: List[str], arms: List[str],
                 context_tokens: int, fullrepo_tokens: int) -> Dict[str, Dict]:
    """Every token/retrieval/latency metric for one task, all arms.

    No LLM here — this half is free and runs without a key.
    """
    # "The whole repo, rendered the way the arms render, with no budget."
    # Measured through the same code path the arms use so the reduction ratio
    # compares like with like, headers and joins included.
    whole = compile_context(index, "fullrepo", seeds, UNLIMITED)
    full_repo_tokens = whole.context_tokens
    total_symbols = len(index.symbols)

    out: Dict[str, Dict] = {}
    for arm in arms:
        budget = fullrepo_tokens if arm == "fullrepo" else context_tokens
        res = compile_context(index, arm, seeds, budget)
        loo = measure_retrieval(index, arm, task, seeds, budget)
        out[arm] = {
            "arm": arm,
            "budget": budget,
            "full_repo_tokens": full_repo_tokens,
            "full_repo_symbols": total_symbols,
            "context_tokens": res.context_tokens,
            "context_symbols": len(res.included),
            "token_reduction_pct": (
                round(100.0 * (1 - res.context_tokens / full_repo_tokens), 3)
                if full_repo_tokens else None),
            # Of the repo's symbols, how many did this arm manage to show?
            # For fullrepo this is the "does it even fit" number.
            "repo_coverage_pct": (round(100.0 * len(res.included) / total_symbols, 2)
                                  if total_symbols else None),
            "truncated": res.truncated,
            "dropped_n": res.dropped_n,
            "ranked_n": res.ranked_n,
            # Per-query, steady state. The dense arm's one-time corpus
            # embedding is charged to corpus_build_ms, NOT here — see
            # providers._LAST_CORPUS_MS for why that split is load-bearing.
            "retrieval_ms": round(res.retrieval_ms, 2),
            "corpus_build_ms": round(res.corpus_build_ms, 2),
            "render_ms": round(res.render_ms, 2),
            **loo,
            "context": res.text,
        }
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def result_key(row: dict) -> tuple:
    return (row.get("model"), row["commit"], row["arm"], row.get("sample", 0))


def load_tasks(path: str) -> List[Task]:
    with open(path, encoding="utf-8") as f:
        return [Task(**t) for t in json.load(f)["tasks"]]


def _make_client(backend: str):
    """Whatever the chosen backend's generate function expects."""
    if backend == "anthropic":
        import anthropic
        return anthropic.Anthropic()
    if backend == "gemini":
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            sys.exit("gemini backend needs GEMINI_API_KEY or GOOGLE_API_KEY")
        return key
    base_url, key_env = OPENAI_COMPAT[backend]
    base_url = os.environ.get("OPENAI_BASE_URL", base_url)
    api_key = os.environ.get(key_env) or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit(f"{backend} backend needs {key_env} or OPENAI_API_KEY")
    return base_url, api_key


def run(args) -> None:
    tasks = load_tasks(args.tasks)
    if args.limit:
        tasks = tasks[:args.limit]
    repo = os.path.abspath(args.repo)
    arms = args.arms.split(",")
    for a in arms:
        if a not in PROVIDERS:
            sys.exit(f"unknown arm {a!r}; known: {PROVIDERS}")

    run_agent = bool(args.backend or args.mock)
    model = None
    if run_agent and not args.mock:
        model = args.model or DEFAULT_MODELS[args.backend]

    os.makedirs(RESULTS_DIR, exist_ok=True)
    tag = f".{args.tag}" if args.tag else ""
    suffix = f".mock.{args.mock}.jsonl" if args.mock else ".jsonl"
    out_path = os.path.join(RESULTS_DIR, os.path.basename(repo) + tag + suffix)

    # Resume: a row counts as done only if it is a real measurement. Rows left
    # by a transient infra failure (429/5xx/network) are retried, never baked
    # in as a failed fix — same rule run_eval.py uses.
    done = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    r = json.loads(ln)
                    if not is_transient_error(r):
                        done.add(result_key(r))

    client = _make_client(args.backend) if (run_agent and not args.mock) else None
    # Namespaced per repo AND per process: sweeping several repos in parallel is
    # the obvious way to use this, and a shared worktree path makes the second
    # run die on "already exists" partway through the first.
    scratch = os.path.join(args.scratch,
                           f"diffcontext-agent-eval-{os.path.basename(repo)}-{os.getpid()}")
    os.makedirs(scratch, exist_ok=True)

    try:
        _sweep(args, tasks, repo, arms, run_agent, model, client, scratch,
               out_path, done)
    finally:
        # pid-namespaced, so this is ours alone to delete.
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"\nwrote {out_path}")


def _sweep(args, tasks, repo, arms, run_agent, model, client, scratch,
           out_path, done) -> None:
    with open(out_path, "a", encoding="utf-8") as out:
        for ti, task in enumerate(tasks):
            # Index the repo AT THE TASK STATE (parent commit). Shared by every
            # arm, and timed — it is a real cost of the retrieval arms that a
            # latency comparison should not hide.
            wt = Worktree(repo, os.path.join(scratch, "ctx-wt"), task.parent)
            try:
                t0 = time.perf_counter()
                index = index_repository(wt.path)
                index_ms = (time.perf_counter() - t0) * 1000
                seeds = [s for s in task.seed_symbols if s in index.symbols]
                if not seeds:
                    print(f"[{ti}] {task.commit[:10]} SKIP: no seed resolvable at parent")
                    continue
                seed_sources = {s: index.symbols[s].code for s in seeds}
                test_diff = _git(repo, "diff", task.parent, task.commit,
                                 "--", *task.test_files).stdout
                # Pay the encoder's model-load and corpus pass BEFORE anything
                # is timed. Otherwise the first task's semantic arm reports a
                # 20s "retrieval" that is really a one-time startup cost, and
                # the arm comparison is off by two orders of magnitude.
                warm_ms = warm_semantic(index) if "semantic" in arms else 0.0
                measured = measure_task(index, task, seeds, arms,
                                        args.context_tokens, args.fullrepo_tokens)
                # The warmup is real work the dense arm has to do; bill it to
                # the semantic arm's corpus column instead of dropping it. On
                # task 0 this is the full model load + corpus pass; afterwards
                # it is only what the new commit actually changed.
                if "semantic" in measured:
                    measured["semantic"]["corpus_build_ms"] = round(
                        measured["semantic"]["corpus_build_ms"] + warm_ms, 2)
            finally:
                wt.remove()

            # Gold-validity gate: re-verify in THIS environment that the gold
            # patch still makes the tests pass before scoring any arm on the
            # task. Only meaningful when an agent is actually being judged.
            if run_agent and not args.mock and not args.skip_gold_gate:
                gv = apply_and_test(repo, task, task.gold_patch, scratch)
                if not gv["passed"]:
                    print(f"[{ti}] {task.commit[:10]} SKIP: gold fails in this env")
                    continue

            for arm in arms:
                m = measured[arm]
                context = m.pop("context")
                for sample in range(args.samples if run_agent else 1):
                    row = {
                        "commit": task.commit, "repo": task.repo,
                        "arm": arm, "sample": sample,
                        "backend": None if args.mock else args.backend,
                        "model": model, "mock": args.mock,
                        "n_seeds": len(seeds), "index_ms": round(index_ms, 1),
                        "semantic_encoder": (semantic_encoder()
                                             if arm == "semantic" else None),
                        "ts": time.time(), **m,
                    }
                    if result_key(row) in done:
                        continue

                    if run_agent:
                        if args.mock:
                            patch = task.gold_patch if args.mock == "gold" else None
                        else:
                            if args.sleep:
                                time.sleep(args.sleep)
                            t0 = time.perf_counter()
                            gen = generate_patch(args.backend, client, model, task,
                                                 seed_sources, context, test_diff)
                            row["generation_ms"] = round(
                                (time.perf_counter() - t0) * 1000, 1)
                            patch = gen["patch"]
                            row.update({"stop_reason": gen["stop_reason"],
                                        "gen_error": gen["error"],
                                        "usage": gen["usage"]})
                            if is_transient_error(row):
                                out.write(json.dumps(row) + "\n")
                                out.flush()
                                print(f"[{ti}] {task.commit[:10]} {arm:12s} "
                                      f"{row['gen_error']} — retry later")
                                continue
                        verdict = apply_and_test(repo, task, patch, scratch)
                        row["agent_success"] = bool(verdict["passed"])
                        row["applied"] = verdict["applied"]
                    else:
                        row["agent_success"] = None

                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    mark = ("PASS" if row["agent_success"] else "fail") \
                        if run_agent else "-"
                    print(f"[{ti}] {task.commit[:10]} {arm:12s} "
                          f"ctx={m['context_tokens']:>7,}tok "
                          f"red={m['token_reduction_pct']:>6.2f}% "
                          f"rec={_fmt(m['context_recall'])} {mark}")


def _fmt(v: Optional[float]) -> str:
    return f"{v:.3f}" if v is not None else "  n/a"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _load(paths: List[str]) -> List[dict]:
    dedup: Dict[tuple, dict] = {}
    n_transient = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                if not ln.strip():
                    continue
                r = json.loads(ln)
                if is_transient_error(r):
                    n_transient += 1
                    continue
                dedup[result_key(r)] = r      # last wins: a retry supersedes
    if n_transient:
        print(f"note: dropped {n_transient} transient-error row(s)\n")
    return list(dedup.values())


def report(paths: List[str]) -> None:
    rows = _load(paths)
    if not rows:
        sys.exit("no valid rows")

    arms = sorted({r["arm"] for r in rows},
                  key=lambda a: DEFAULT_ARMS.index(a) if a in DEFAULT_ARMS else 99)
    tasks = {r["commit"] for r in rows}
    repos = sorted({r["repo"] for r in rows})
    encoders = {r["semantic_encoder"] for r in rows if r.get("semantic_encoder")}

    print(f"===== agent eval: {', '.join(repos)} =====")
    print(f"{len(rows)} rows, {len(tasks)} tasks, {len(arms)} arms")
    loo_tasks = {r["commit"] for r in rows if r.get("loo_eligible")}
    print(f"leave-one-out retrieval scored on {len(loo_tasks)}/{len(tasks)} tasks "
          f"(the rest changed a single symbol — nothing to hold out)")
    if encoders:
        enc = ", ".join(sorted(encoders))
        print(f"semantic arm encoder: {enc}")
        if any("tfidf" in e for e in encoders):
            print("  !! LEXICAL FALLBACK — this is not dense retrieval and must "
                  "not be cited as a semantic baseline (install "
                  "sentence-transformers)")
    print()

    hdr = (f"{'arm':<14}{'repo tok':>11}{'ctx tok':>10}{'reduc':>9}"
           f"{'prec':>8}{'recall':>8}{'success':>9}{'ret ms':>9}")
    print(hdr)
    print("-" * len(hdr))
    for arm in arms:
        ar = [r for r in rows if r["arm"] == arm]
        prec = _mean([r["context_precision"] for r in ar
                      if r.get("context_precision") is not None])
        rec = _mean([r["context_recall"] for r in ar
                     if r.get("context_recall") is not None])
        succ = _mean([1.0 if r["agent_success"] else 0.0 for r in ar
                      if r.get("agent_success") is not None])
        print(f"{arm:<14}"
              f"{statistics.mean(r['full_repo_tokens'] for r in ar):>11,.0f}"
              f"{statistics.mean(r['context_tokens'] for r in ar):>10,.0f}"
              f"{statistics.mean(r['token_reduction_pct'] for r in ar):>8.2f}%"
              f"{_fmt(prec):>8}{_fmt(rec):>8}"
              f"{(_fmt(succ) if succ is not None else '   n/a'):>9}"
              f"{statistics.mean(r['retrieval_ms'] for r in ar):>9.1f}")

    print("\n'ret ms' is per-query, steady state. Corpus preparation is a "
          "separate, amortized cost:")
    for arm in arms:
        ar = [r for r in rows if r["arm"] == arm]
        idx = statistics.mean(r.get("index_ms") or 0.0 for r in ar)
        note = f"  {arm:<14} shared repo index {idx:8.0f} ms"
        build = sorted(r.get("corpus_build_ms") or 0.0 for r in ar)
        if any(build):
            # Cold and warm reported separately, never averaged: the mean of a
            # one-time 20s model load and a dozen sub-second incremental passes
            # describes neither the first query nor the steady state.
            note += (f" + corpus embedding {build[-1]:8.0f} ms cold, "
                     f"{statistics.median(build):.0f} ms/task after")
        print(note)

    # Truncation: the fullrepo arm's headline fact. An arm that had to drop
    # candidates did not show the model what it wanted to, and a reduction
    # number from a truncated arm is a budget artifact, not a selection result.
    print()
    for arm in arms:
        ar = [r for r in rows if r["arm"] == arm]
        trunc = [r for r in ar if r.get("truncated")]
        if trunc:
            cov = statistics.mean(r["repo_coverage_pct"] for r in trunc)
            print(f"{arm}: budget-truncated on {len(trunc)}/{len(ar)} rows "
                  f"(showed {cov:.1f}% of repo symbols on those)")
        else:
            print(f"{arm}: never truncated — the whole ranked set fit in "
                  f"{statistics.mean(r['budget'] for r in ar):,.0f} tokens")

    _retrieval_significance(rows, arms)
    _agent_significance(rows, arms)


def _retrieval_significance(rows: List[dict], arms: List[str]) -> None:
    """Paired Wilcoxon on leave-one-out recall and precision.

    Retrieval is deterministic, so unlike the agent pass there is no sampling
    noise to average out — but the task sample is still small, and a mean
    difference over a handful of tasks is not evidence. Pairing is by commit:
    every arm sees the identical task, so the comparison is within-task.
    """
    for metric in ("context_recall", "context_precision"):
        by: Dict[tuple, float] = {}
        for r in rows:
            if r.get(metric) is not None:
                by[(r["arm"], r["commit"])] = r[metric]
        present = [a for a in arms if any(k[0] == a for k in by)]
        commits = sorted({c for _, c in by})
        common = [c for c in commits if all((a, c) in by for a in present)]
        if len(present) < 2 or not common:
            continue

        means = {a: statistics.mean(by[(a, c)] for c in common) for a in present}
        ordered = sorted(present, key=lambda a: -means[a])
        print(f"\n{metric} paired over {len(common)} leave-one-out tasks: "
              + ", ".join(f"{a} {means[a]:.3f}" for a in ordered))
        if len(common) < 6:
            print("  (too few for a paired test — need >= 6)")
            continue
        # ALL pairs, not just each arm against the leader. On recall the leader
        # is always `fullrepo` — it shows everything, so it cannot lose — and
        # testing only against it would silently omit the comparison the eval
        # exists to make: retrieval vs retrieval at equal budget.
        out, ps = [], []
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                x = [by[(a, c)] for c in common]
                y = [by[(b, c)] for c in common]
                _, pval, n_eff = wilcoxon_signed_rank(x, y)
                out.append((a, b, means[a] - means[b], pval, n_eff))
                ps.append(pval)
        for (a, b, delta, pval, n_eff), adj in zip(out, holm_bonferroni(ps)):
            verdict = "" if adj < 0.05 else "   (not significant)"
            print(f"  {a:12s} vs {b:12s} delta={delta:+.3f}  p={pval:.4f}  "
                  f"holm={adj:.4f}  (n_eff={n_eff}){verdict}")


def _agent_significance(rows: List[dict], arms: List[str]) -> None:
    """Paired comparison of agent success, when an agent actually ran.

    Paired unit is (model, commit) — the same rule run_eval.py settled on. A
    pair must never straddle two models, or model disagreement reads as arm
    discrimination.
    """
    scored = [r for r in rows if r.get("agent_success") is not None]
    if not scored:
        print("\nagent success rate: not measured (run with --backend or --mock)")
        return

    by: Dict[tuple, List[float]] = {}
    for r in scored:
        by.setdefault((r["arm"], (r.get("model"), r["commit"])), []) \
          .append(1.0 if r["agent_success"] else 0.0)
    present = [a for a in arms if any(k[0] == a for k in by)]
    units = sorted({k[1] for k in by}, key=lambda u: (str(u[0]), str(u[1])))
    common = [u for u in units if all((a, u) in by for a in present)]
    print(f"\nagent success paired over {len(common)} tasks "
          f"with every arm present")
    if not common or len(present) < 2:
        return

    means = {a: statistics.mean(statistics.mean(by[(a, u)]) for u in common)
             for a in present}

    # Discrimination: a paired test can only see tasks where arms disagree.
    ceiling = sum(1 for u in common
                  if all(statistics.mean(by[(a, u)]) == 1.0 for a in present))
    floor = sum(1 for u in common
                if all(statistics.mean(by[(a, u)]) == 0.0 for a in present))
    informative = len(common) - ceiling - floor
    print(f"discrimination: {informative}/{len(common)} tasks separate the arms "
          f"({ceiling} ceiling, {floor} floor)")
    if not informative:
        print("  !! no task distinguishes any arm — this result set cannot "
              "support ANY claim about context quality, in either direction")
        return

    if len(common) < 6:
        print("(too few complete tasks for a paired test — need >= 6)")
        return
    ordered = sorted(present, key=lambda a: -means[a])
    top = ordered[0]
    x = [statistics.mean(by[(top, u)]) for u in common]
    out, ps = [], []
    for a in ordered[1:]:
        y = [statistics.mean(by[(a, u)]) for u in common]
        _, pval, n_eff = wilcoxon_signed_rank(x, y)
        out.append((a, means[top] - means[a], pval, n_eff))
        ps.append(pval)
    print("\nPaired Wilcoxon vs. top arm, Holm-corrected:")
    for (a, delta, pval, n_eff), adj in zip(out, holm_bonferroni(ps)):
        print(f"  {top} vs {a:14s} delta={delta:+.3f}  p={pval:.4f}  "
              f"holm={adj:.4f}  (n_eff={n_eff})")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", help="tasks JSON from downstream/tasks.py")
    ap.add_argument("--repo", help="path to the benchmark repo clone")
    ap.add_argument("--arms", default=",".join(DEFAULT_ARMS),
                    help=f"comma-separated arms; known: {','.join(PROVIDERS)} "
                         f"(default: {','.join(DEFAULT_ARMS)})")
    ap.add_argument("--context-tokens", type=int, default=DEFAULT_CONTEXT_TOKENS,
                    help="budget for the retrieval arms")
    ap.add_argument("--fullrepo-tokens", type=int, default=DEFAULT_FULLREPO_TOKENS,
                    help="budget for the fullrepo arm; kept separate so "
                         "'paste the codebase' is not reduced to 'the first "
                         "few files alphabetically'")
    ap.add_argument("--backend",
                    choices=["anthropic", "gemini", "groq", "openrouter", "mistral"],
                    help="enable the agent-success metric by generating patches")
    ap.add_argument("--model", default=None)
    ap.add_argument("--samples", type=int, default=1,
                    help="generations per (task, arm)")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="pause before each generation, for free-tier rate limits")
    ap.add_argument("--mock", choices=["gold", "empty"],
                    help="self-test the judge without an LLM: gold must PASS "
                         "every task on every arm, empty must FAIL every one")
    ap.add_argument("--skip-gold-gate", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="measure only the first N tasks (smoke tests)")
    ap.add_argument("--tag", default=None,
                    help="suffix for the results filename; keep one tag per model")
    ap.add_argument("--scratch", default=os.environ.get("TMPDIR", "/tmp"))
    ap.add_argument("--report", metavar="RESULTS_JSONL", nargs="+")
    args = ap.parse_args()

    if args.report:
        report(args.report)
        return
    if not args.tasks or not args.repo:
        ap.error("--tasks and --repo are required unless --report")
    run(args)


if __name__ == "__main__":
    main()
