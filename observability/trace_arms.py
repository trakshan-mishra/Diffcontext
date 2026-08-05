#!/usr/bin/env python3
"""
trace_arms.py — run DiffContext's context providers under Neatlogs tracing.

Each (task, provider) pair becomes one WORKFLOW trace in the Neatlogs dashboard:

    WORKFLOW  diffcontext_arm            tags: provider:<arm>, repo:<name>
    ├── CHAIN      index_repository       (cached after first run)
    ├── RETRIEVER  rank_symbols           ← the arm under test
    ├── CHAIN      pack_token_budget      ← how much survived the budget
    └── LLM        (auto-captured)        ← only with --llm

Same task, same budget, same seeds — only the context provider changes. That is
DiffContext's whole thesis, and Neatlogs renders it as five comparable traces
with token counts, latency, and (with --llm) real cost per arm.

Usage
-----
    export NEATLOGS_API_KEY=...          # or put it in observability/.env

    # No LLM key needed — traces the retrieval + packing pipeline only
    python observability/trace_arms.py --repo /tmp/reqrepo

    # With a real LLM call per arm (OpenAI-compatible or Anthropic)
    export GROQ_API_KEY=...
    python observability/trace_arms.py --repo /tmp/reqrepo --llm groq

Backends: groq, openrouter, mistral, openai (OpenAI-compatible SDK),
          anthropic (Anthropic SDK). All are wrapped via neatlogs.wrap().
"""

import argparse
import os
import sys
import time

# ---------------------------------------------------------------------------
# 1. Load .env (optional) and init Neatlogs BEFORE importing anything traced.
#    Auto-instrumentation patches libraries at import time, so init() must run
#    first or the LLM/HTTP spans silently never appear.
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

if not os.environ.get("NEATLOGS_API_KEY"):
    sys.exit(
        "NEATLOGS_API_KEY is not set.\n"
        "  Get the project API key from https://app.neatlogs.com (Quick setup -> "
        "Open full setup -> Project API key -> show), then either:\n"
        "    export NEATLOGS_API_KEY=...\n"
        "  or write it into observability/.env (already gitignored)."
    )

import neatlogs  # noqa: E402
from neatlogs import span  # noqa: E402


def _init_neatlogs(instrumentations, tags):
    # init() ignores $NEATLOGS_ENDPOINT (see README finding 3) — pass it
    # explicitly, or a run meant for the local capture goes to production.
    init_kwargs = dict(
        api_key=os.environ["NEATLOGS_API_KEY"],
        workflow_name="diffcontext-arms",
        instrumentations=instrumentations,
        tags=tags,
        debug=os.environ.get("NEATLOGS_DEBUG") == "1",
    )
    forced = os.environ.get("NEATLOGS_ENDPOINT_FORCE")
    if forced:
        init_kwargs["endpoint"] = forced

    neatlogs.init(**init_kwargs)


# ---------------------------------------------------------------------------
# 2. Instrumented pipeline stages.
#    Imports of diffcontext happen inside main(), after init().
# ---------------------------------------------------------------------------

@span(kind="CHAIN", name="index_repository")
def do_index(repo_path):
    from diffcontext.pipeline import index_repository

    t0 = time.perf_counter()
    index = index_repository(repo_path)
    neatlogs.log(
        "indexed {symbols} symbols in {ms}ms",
        symbols=len(index.symbols),
        ms=round((time.perf_counter() - t0) * 1000, 1),
        repo=repo_path,
    )
    return index


@span(kind="RETRIEVER", name="rank_symbols")
def do_rank(index, provider, seeds):
    """The arm under test. RETRIEVER is the right OpenInference kind here —
    this is retrieval over a symbol corpus, just graph-based rather than
    embedding-based."""
    from benchmarks.downstream.providers import RANKERS

    t0 = time.perf_counter()
    ranked = RANKERS[provider](index, seeds)
    neatlogs.log(
        "{provider} ranked {n} candidates",
        provider=provider,
        n=len(ranked),
        seeds=len(seeds),
        ms=round((time.perf_counter() - t0) * 1000, 1),
        top5=ranked[:5],
    )
    return ranked


@span(kind="CHAIN", name="pack_token_budget")
def do_pack(index, ranked, max_tokens):
    """Where DiffContext's actual value shows up: how much of the ranking
    survives the budget, and how much got dropped."""
    from benchmarks.downstream.providers import render_context, _estimate_tokens

    import re

    context = render_context(index, ranked, max_tokens)
    used = _estimate_tokens(context) if context else 0
    kept = len(re.findall(r"^# \./", context, re.M))

    neatlogs.log(
        "packed {kept}/{ranked} symbols into {used}/{budget} tokens",
        kept=kept,
        ranked=len(ranked),
        dropped=max(0, len(ranked) - kept),
        used=used,
        budget=max_tokens,
        utilization=round(used / max_tokens, 3) if max_tokens else 0,
        chars=len(context),
    )
    return context, used, kept


# ---------------------------------------------------------------------------
# 3. Optional LLM arm.
# ---------------------------------------------------------------------------

OPENAI_COMPATIBLE = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "deepseek/deepseek-chat"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", "devstral-2512"),
    "openai": (None, "OPENAI_API_KEY", "gpt-4o-mini"),
}

PROMPT = (
    "You are given repository context selected by an automated tool.\n"
    "Using ONLY this context, explain what would break if the seed function's "
    "signature changed. Be specific and cite symbol names.\n\n"
    "=== CONTEXT ===\n{context}\n=== END CONTEXT ===\n\nSeeds: {seeds}"
)


def build_llm_client(backend):
    if backend == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("anthropic backend needs ANTHROPIC_API_KEY")
        import anthropic

        return neatlogs.wrap(anthropic.Anthropic()), "claude-sonnet-4-5"

    base_url, key_env, default_model = OPENAI_COMPATIBLE[backend]
    if not os.environ.get(key_env):
        sys.exit(f"{backend} backend needs {key_env}")
    from openai import OpenAI

    kwargs = {"api_key": os.environ[key_env]}
    if base_url:
        kwargs["base_url"] = base_url
    return neatlogs.wrap(OpenAI(**kwargs)), default_model


def call_llm(client, backend, model, context, seeds):
    text = PROMPT.format(context=context or "(no context provided)", seeds=", ".join(seeds))
    if backend == "anthropic":
        return client.messages.create(
            model=model, max_tokens=600, messages=[{"role": "user", "content": text}]
        )
    return client.chat.completions.create(
        model=model, max_tokens=600, messages=[{"role": "user", "content": text}]
    )


# ---------------------------------------------------------------------------
# 4. One arm = one trace.
# ---------------------------------------------------------------------------

@span(kind="WORKFLOW", name="diffcontext_arm")
def run_arm(index, provider, seeds, max_tokens, llm=None):
    ranked = do_rank(index, provider, seeds)
    context, used, kept = do_pack(index, ranked, max_tokens)

    if llm is not None:
        client, backend, model = llm
        call_llm(client, backend, model, context, seeds)

    return {"provider": provider, "ranked": len(ranked), "kept": kept, "tokens": used}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/tmp/reqrepo",
                    help="repo to index (use a COPY — see note in README)")
    ap.add_argument("--seeds", nargs="+",
                    default=["./src/requests/sessions.py:Session.request"])
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--providers", nargs="+", default=None,
                    help="default: all five arms")
    ap.add_argument("--llm", default=None,
                    choices=list(OPENAI_COMPATIBLE) + ["anthropic"],
                    help="also make one real LLM call per arm")
    args = ap.parse_args()

    instrumentations = []
    if args.llm == "anthropic":
        instrumentations = ["anthropic"]
    elif args.llm:
        instrumentations = ["openai"]

    _init_neatlogs(instrumentations, tags=[f"repo:{os.path.basename(args.repo)}",
                                           f"budget:{args.max_tokens}"])

    llm = None
    if args.llm:
        client, model = build_llm_client(args.llm)
        llm = (client, args.llm, model)

    from benchmarks.downstream.providers import PROVIDERS

    providers = args.providers or PROVIDERS

    index = do_index(args.repo)
    seeds = [s for s in args.seeds if s in index.symbols]
    if not seeds:
        sys.exit(f"none of {args.seeds} are in the index. "
                 f"Sample ids: {list(index.symbols)[:3]}")

    print(f"repo={args.repo}  symbols={len(index.symbols)}  seeds={seeds}")
    print(f"budget={args.max_tokens}  arms={providers}  llm={args.llm or 'off'}\n")

    rows = []
    for provider in providers:
        row = run_arm(index, provider, seeds, args.max_tokens, llm)
        rows.append(row)
        print(f"  {row['provider']:<16} ranked={row['ranked']:<5} "
              f"kept={row['kept']:<4} tokens={row['tokens']}")

    # Scripts: flush then shutdown. (Servers: init() once, never per-request.)
    neatlogs.flush()
    neatlogs.shutdown()

    print("\nTraces sent. Open https://app.neatlogs.com and compare the arms.")
    print("Try in AI Search:  which provider used the most context tokens?")


if __name__ == "__main__":
    main()
