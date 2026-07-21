#!/usr/bin/env python3
"""
run_eval.py — the rung-5 downstream eval: same model, same prompt, same
token budget, same oracle-localized seeds; ONLY the context provider
changes. Judge = the repo's own tests (mined and machine-validated by
tasks.py).

Modes:
  --mock gold    apply the gold patch instead of calling an LLM (harness
                 self-test: every task must PASS, or the judge is broken)
  --mock empty   apply nothing (self-test: every task must FAIL)
  (default)      generate patches with a real LLM. Choose the provider with
                 --backend; the model is held FIXED across all arms, so only
                 the provider context block varies:
                   anthropic (default)  ANTHROPIC_API_KEY / `ant auth login`
                   gemini               GEMINI_API_KEY or GOOGLE_API_KEY

Results append to benchmarks/downstream/results/<repo>.jsonl (resumable:
already-recorded (task, provider, sample) rows are skipped). Summarize
with --report, which prints per-provider pass rates and paired Wilcoxon
tests (Holm-corrected) against every other provider.

Cost: ~10-14k input + ~2k output tokens per generation on claude-opus-4-8
=> roughly $0.10/generation; 20 tasks x 5 providers x 1 sample ~ $10.
Prompt caching cuts this substantially: the per-task prefix (instructions,
test diff, failing output, seed sources) is byte-identical across arms and
cached; only the provider context block differs.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from benchmarks.downstream.tasks import Task, Worktree, _git, _run_tests
from benchmarks.downstream.providers import PROVIDERS, compile_provider_context
from benchmarks.significance import holm_bonferroni, wilcoxon_signed_rank
from diffcontext.pipeline import index_repository

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
DEFAULT_MODELS = {"anthropic": "claude-opus-4-8", "gemini": "gemini-2.5-pro",
                  "openrouter": "deepseek/deepseek-chat"}
DEFAULT_CONTEXT_TOKENS = 8000
MAX_OUTPUT_TOKENS = 16000
GEMINI_MAX_RETRIES = 5  # transient 429 (per-minute rate limit) backoff attempts
OPENROUTER_MAX_RETRIES = 5  # same: transient 429 backoff for the OpenAI-compatible path

SYSTEM_PROMPT = (
    "You are an expert Python developer fixing a failing test suite. "
    "You receive the failing tests, the test changes that introduced them, the "
    "current source of the functions that must change, and repository context. "
    "Respond with ONLY a unified diff (git apply -p1 format, a/ and b/ path "
    "prefixes, paths relative to the repository root) that modifies the "
    "production code so the tests pass. Do not modify test files. Do not "
    "include any prose outside the diff. Wrap the diff in ```diff fences."
)


# ---------------------------------------------------------------------------
# Prompt assembly — everything except `context` is identical across arms.
# The provider context goes LAST so the per-task prefix is byte-identical
# across arms and prompt-cacheable.
# ---------------------------------------------------------------------------

FINAL_INSTRUCTION = "\nProduce the unified diff that fixes the failing tests."


def _prompt_parts(task: Task, seed_sources: Dict[str, str], context: str,
                  test_diff: str) -> tuple:
    """(arm-invariant task material, provider context block).

    Split so both backends assemble byte-identical text and the Anthropic
    arm can place a cache breakpoint between the two halves.
    """
    fixed = (
        f"Repository: {task.repo}\n"
        f"Failing test files: {', '.join(task.test_files)}\n\n"
        f"## Test changes that introduced the failures\n```diff\n{test_diff[:6000]}\n```\n\n"
        f"## Failing test output (tail)\n```\n{task.fail_output[-2500:]}\n```\n\n"
        f"## Functions that must change (current source)\n"
        + "".join(f"### {sid}\n```python\n{src}\n```\n" for sid, src in seed_sources.items())
    )
    context_block = (
        f"\n## Repository context (related code, may help)\n{context}\n"
        if context else "\n## Repository context\n(none provided)\n"
    )
    return fixed, context_block


def build_messages(task: Task, seed_sources: Dict[str, str], context: str,
                   test_diff: str) -> List[dict]:
    fixed, context_block = _prompt_parts(task, seed_sources, context, test_diff)
    return [{
        "role": "user",
        "content": [
            # cache breakpoint after the arm-invariant task material
            {"type": "text", "text": fixed, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": context_block + FINAL_INSTRUCTION},
        ],
    }]


def extract_diff(text: str) -> Optional[str]:
    m = re.search(r"```diff\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"^(---\s.*)", text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else None


def generate_patch(backend: str, client, model: str, task: Task,
                   seed_sources: Dict[str, str], context: str,
                   test_diff: str) -> dict:
    """One LLM generation on the chosen backend.

    Returns {'patch', 'stop_reason', 'usage', 'error'}. The two backends
    receive identical prompt text; only the wire format differs.
    """
    if backend == "gemini":
        return _generate_gemini(client, model, task, seed_sources, context, test_diff)
    if backend == "openrouter":
        return _generate_openrouter(client, model, task, seed_sources, context, test_diff)
    return _generate_anthropic(
        client, model, build_messages(task, seed_sources, context, test_diff))


def _generate_anthropic(client, model: str, messages: List[dict]) -> dict:
    with client.messages.stream(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        return {"patch": None, "stop_reason": "refusal", "error": "refused",
                "usage": msg.usage.to_dict()}
    text = "".join(b.text for b in msg.content if b.type == "text")
    return {"patch": extract_diff(text), "stop_reason": msg.stop_reason,
            "error": None if extract_diff(text) else "no_diff_in_output",
            "usage": msg.usage.to_dict()}


def _generate_gemini(client, model: str, task: Task,
                     seed_sources: Dict[str, str], context: str,
                     test_diff: str) -> dict:
    """Gemini generation via the google-genai SDK.

    The prompt is one text turn (SYSTEM_PROMPT goes in system_instruction).
    Gemini has its own context caching, but we don't use it here: the
    eval's correctness doesn't depend on caching, only its cost, and
    skipping it keeps the per-arm request payloads simple and identical.
    """
    from google.genai import types
    fixed, context_block = _prompt_parts(task, seed_sources, context, test_diff)
    prompt = fixed + context_block + FINAL_INSTRUCTION
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT, max_output_tokens=MAX_OUTPUT_TOKENS)

    # Retry transient 429s (per-minute rate limits) so a rate cap never gets
    # silently recorded as a failed fix. Honor the server's retryDelay when
    # present; bail immediately on a hard per-day quota (retrying can't help).
    resp = None
    for attempt in range(GEMINI_MAX_RETRIES + 1):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
            break
        except Exception as e:  # network/quota/bad-key
            msg = str(e)
            is_429 = getattr(e, "code", None) == 429 or "429" in msg or "RESOURCE_EXHAUSTED" in msg
            hard_daily = "PerDay" in msg or "per day" in msg.lower()
            if is_429 and not hard_daily and attempt < GEMINI_MAX_RETRIES:
                m = re.search(r"retryDelay['\"]?:?\s*['\"]?(\d+)", msg)
                delay = min(int(m.group(1)) + 1, 60) if m else min(5 * 2 ** attempt, 60)
                time.sleep(delay)
                continue
            return {"patch": None, "stop_reason": "error",
                    "error": f"api_error:{type(e).__name__}"
                             + (":per_day_quota" if hard_daily else ""),
                    "usage": {}}

    um = getattr(resp, "usage_metadata", None)
    usage = ({"input_tokens": getattr(um, "prompt_token_count", None),
              "output_tokens": getattr(um, "candidates_token_count", None),
              "total_tokens": getattr(um, "total_token_count", None)}
             if um is not None else {})

    fb = getattr(resp, "prompt_feedback", None)
    if fb is not None and getattr(fb, "block_reason", None):
        return {"patch": None, "stop_reason": f"blocked:{fb.block_reason}",
                "error": "refused", "usage": usage}

    cand = (getattr(resp, "candidates", None) or [None])[0]
    stop = str(getattr(cand, "finish_reason", None)) if cand is not None else "no_candidate"
    try:
        text = resp.text or ""
    except Exception:
        text = ""  # a blocked/empty candidate makes .text raise
    diff = extract_diff(text)
    return {"patch": diff, "stop_reason": stop,
            "error": None if diff else "no_diff_in_output", "usage": usage}


def _generate_openrouter(client, model: str, task: Task,
                         seed_sources: Dict[str, str], context: str,
                         test_diff: str) -> dict:
    """OpenRouter generation via the OpenAI-compatible SDK.

    OpenRouter exposes an OpenAI chat-completions API at a custom base_url, so
    one client reaches any listed model (DeepSeek, Qwen, GPT, Llama, ...).
    SYSTEM_PROMPT is the system turn; the task material + context is one user
    turn, byte-identical across arms except the provider context block.
    """
    fixed, context_block = _prompt_parts(task, seed_sources, context, test_diff)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": fixed + context_block + FINAL_INSTRUCTION},
    ]
    resp = None
    for attempt in range(OPENROUTER_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, max_tokens=MAX_OUTPUT_TOKENS)
            break
        except Exception as e:  # network / rate limit / bad key
            is_429 = getattr(e, "status_code", None) == 429 or "429" in str(e)
            if is_429 and attempt < OPENROUTER_MAX_RETRIES:
                time.sleep(min(5 * 2 ** attempt, 60))
                continue
            return {"patch": None, "stop_reason": "error",
                    "error": f"api_error:{type(e).__name__}", "usage": {}}

    choice = resp.choices[0] if resp.choices else None
    text = (choice.message.content or "") if choice is not None else ""
    finish = getattr(choice, "finish_reason", "no_candidate") if choice else "no_candidate"
    u = getattr(resp, "usage", None)
    usage = ({"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens,
              "total_tokens": u.total_tokens} if u is not None else {})
    diff = extract_diff(text)
    return {"patch": diff, "stop_reason": str(finish),
            "error": None if diff else "no_diff_in_output", "usage": usage}


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

# Applier cascade: LLM diffs frequently have off-by-a-line hunk headers or
# imperfect surrounding context, which strict `git apply` rejects outright.
# Try progressively more tolerant strategies before declaring a patch unusable,
# ordered cleanest-first so a well-formed diff still applies exactly; recount
# fixes wrong @@ counts, 3way merges via blob context, `patch --fuzz` is the
# last resort for imperfect context. Recording which strategy won also tells us
# how "clean" each backend's diffs are.
APPLY_STRATEGIES = (
    ("git-p1",         ["git", "apply", "-p1", "--whitespace=nowarn"]),
    ("git-p1-recount", ["git", "apply", "-p1", "--recount", "--whitespace=nowarn"]),
    ("git-p1-3way",    ["git", "apply", "-p1", "--3way", "--whitespace=nowarn"]),
    ("git-p0",         ["git", "apply", "-p0", "--whitespace=nowarn"]),
    ("patch-p1-fuzz",  ["patch", "-p1", "--fuzz=3", "-f", "--no-backup-if-mismatch"]),
    ("patch-p0-fuzz",  ["patch", "-p0", "--fuzz=3", "-f", "--no-backup-if-mismatch"]),
)


def _apply_patch(wt: Worktree, task: Task, patch: str) -> tuple:
    """Apply `patch`, trying each strategy on a freshly-reset task state
    (patch(1) is not atomic, so a failed attempt can leave a dirty tree).
    Returns (strategy_name, None) on success, or (None, last_error).
    """
    last_err = ""
    for name, cmd in APPLY_STRATEGIES:
        wt.checkout(task.parent)
        wt.overlay_files(task.commit, task.test_files)
        try:
            r = subprocess.run(cmd, cwd=wt.path, input=patch,
                               capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            last_err = f"{name}: timeout"
            continue
        if r.returncode == 0:
            return name, None
        last_err = f"{name}: {(r.stderr or r.stdout).strip()[-200:]}"
    wt.checkout(task.parent)            # leave a clean, patch-free state behind
    wt.overlay_files(task.commit, task.test_files)
    return None, last_err


def apply_and_test(repo: str, task: Task, patch: Optional[str], scratch: str) -> dict:
    """Build the task state, apply the patch, run the task's tests."""
    wt = Worktree(repo, os.path.join(scratch, "judge-wt"), task.parent)
    try:
        wt.overlay_files(task.commit, task.test_files)
        strategy = None
        if patch:
            strategy, apply_err = _apply_patch(wt, task, patch)
            if strategy is None:
                return {"applied": False, "passed": False,
                        "detail": "patch_apply_failed", "apply_error": apply_err}
        try:
            res = _run_tests(wt.path, task.test_files)
        except subprocess.TimeoutExpired:
            return {"applied": patch is not None, "passed": False,
                    "detail": "test_timeout", "apply_strategy": strategy}
        return {"applied": patch is not None, "passed": res.returncode == 0,
                "detail": (res.stdout + res.stderr)[-500:],
                "apply_strategy": strategy}
    finally:
        wt.remove()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def load_tasks(path: str) -> List[Task]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [Task(**t) for t in data["tasks"]]


def result_key(row: dict) -> tuple:
    return (row["commit"], row["provider"], row["sample"])


def run(args) -> None:
    tasks = load_tasks(args.tasks)
    repo = os.path.abspath(args.repo)
    providers = args.providers.split(",")
    for p in providers:
        if p not in PROVIDERS:
            sys.exit(f"unknown provider {p!r}; known: {PROVIDERS}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = os.path.join(
        RESULTS_DIR, os.path.basename(repo) + (".mock.jsonl" if args.mock else ".jsonl"))
    done = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            done = {result_key(json.loads(ln)) for ln in f if ln.strip()}

    if args.model is None:
        args.model = DEFAULT_MODELS[args.backend]

    client = None
    if not args.mock:
        if args.backend == "anthropic":
            import anthropic
            client = anthropic.Anthropic()  # ANTHROPIC_API_KEY or ant auth profile
        elif args.backend == "gemini":
            from google import genai
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not api_key:
                sys.exit("gemini backend needs GEMINI_API_KEY or GOOGLE_API_KEY "
                         "in the environment")
            client = genai.Client(api_key=api_key)
        else:  # openrouter (OpenAI-compatible gateway)
            from openai import OpenAI
            api_key = os.environ.get("OPENROUTER_API_KEY")
            if not api_key:
                sys.exit("openrouter backend needs OPENROUTER_API_KEY in the environment")
            client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)

    scratch = os.path.join(args.scratch, "diffcontext-downstream")
    os.makedirs(scratch, exist_ok=True)

    with open(out_path, "a", encoding="utf-8") as out:
        for ti, task in enumerate(tasks):
            # Task-state index + seed sources + test diff: built once per task,
            # shared by every arm.
            wt = Worktree(repo, os.path.join(scratch, "ctx-wt"), task.parent)
            try:
                index = index_repository(wt.path)
                seeds = [s for s in task.seed_symbols if s in index.symbols]
                if not seeds:
                    print(f"[{ti}] {task.commit[:10]} SKIP: no seed resolvable at parent")
                    continue
                seed_sources = {s: index.symbols[s].code for s in seeds}
                test_diff = _git(repo, "diff", task.parent, task.commit,
                                 "--", *task.test_files).stdout
                contexts = {
                    p: compile_provider_context(index, p, seeds, args.context_tokens)
                    for p in providers
                }
            finally:
                wt.remove()

            for provider in providers:
                for sample in range(args.samples):
                    key = (task.commit, provider, sample)
                    if key in done:
                        continue
                    row = {"commit": task.commit, "repo": task.repo,
                           "provider": provider, "sample": sample,
                           "backend": None if args.mock else args.backend,
                           "model": None if args.mock else args.model,
                           "context_tokens_budget": args.context_tokens,
                           "n_seeds": len(seeds), "ts": time.time()}

                    if args.mock == "gold":
                        patch = task.gold_patch
                        row.update({"mock": "gold"})
                    elif args.mock == "empty":
                        patch = None
                        row.update({"mock": "empty"})
                    else:
                        gen = generate_patch(
                            args.backend, client, args.model, task,
                            seed_sources, contexts[provider], test_diff)
                        patch = gen["patch"]
                        row.update({"stop_reason": gen["stop_reason"],
                                    "gen_error": gen["error"],
                                    "usage": gen["usage"],
                                    "patch": (gen["patch"] or "")[:4000]})

                    verdict = apply_and_test(repo, task, patch, scratch)
                    row.update(verdict)
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    mark = "PASS" if verdict["passed"] else "fail"
                    print(f"[{ti}] {task.commit[:10]} {provider:16s} s{sample} {mark}")


# ---------------------------------------------------------------------------
# Reporting — paired per-task comparison, Wilcoxon + Holm
# ---------------------------------------------------------------------------

def report(path: str) -> None:
    rows = []
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    if not rows:
        sys.exit("no rows")

    # per (provider, commit): mean pass over samples
    by_pc: Dict[tuple, List[float]] = {}
    for r in rows:
        by_pc.setdefault((r["provider"], r["commit"]), []).append(1.0 if r["passed"] else 0.0)
    providers = sorted({p for p, _ in by_pc})
    commits = sorted({c for _, c in by_pc})
    common = [c for c in commits if all((p, c) in by_pc for p in providers)]

    print(f"{len(rows)} rows, {len(commits)} tasks, {len(common)} with every provider\n")
    print(f"{'provider':18s} {'pass rate':>9s}   (paired over {len(common)} tasks)")
    means = {}
    for p in providers:
        vals = [sum(by_pc[(p, c)]) / len(by_pc[(p, c)]) for c in common]
        means[p] = sum(vals) / len(vals) if vals else 0.0
        print(f"{p:18s} {means[p]:9.3f}")

    if len(common) >= 6 and len(providers) >= 2:
        print("\nPaired Wilcoxon (two-sided), Holm-corrected within this family:")
        pairs, ps = [], []
        ordered = sorted(providers, key=lambda p: -means[p])
        primary = ordered[0]
        x = [sum(by_pc[(primary, c)]) / len(by_pc[(primary, c)]) for c in common]
        for p in ordered[1:]:
            y = [sum(by_pc[(p, c)]) / len(by_pc[(p, c)]) for c in common]
            _, pval, n_eff = wilcoxon_signed_rank(x, y)
            pairs.append((primary, p, pval, n_eff))
            ps.append(pval)
        adj = holm_bonferroni(ps)
        for (a, b, pval, n_eff), ap in zip(pairs, adj):
            print(f"  {a} vs {b:16s} p={pval:.4f}  holm={ap:.4f}  (n_eff={n_eff})")
    else:
        print("\n(too few complete tasks for a paired test — need >= 6)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", help="tasks JSON from tasks.py")
    ap.add_argument("--repo", help="path to the benchmark repo clone")
    ap.add_argument("--providers", default=",".join(PROVIDERS))
    ap.add_argument("--backend", choices=["anthropic", "gemini", "openrouter"], default="anthropic",
                    help="LLM provider for generation (mock modes ignore this)")
    ap.add_argument("--model", default=None,
                    help=f"model id; defaults per --backend: {DEFAULT_MODELS}")
    ap.add_argument("--samples", type=int, default=1,
                    help="generations per (task, provider)")
    ap.add_argument("--context-tokens", type=int, default=DEFAULT_CONTEXT_TOKENS)
    ap.add_argument("--mock", choices=["gold", "empty"],
                    help="harness self-test instead of LLM calls")
    ap.add_argument("--scratch", default=os.environ.get("TMPDIR", "/tmp"))
    ap.add_argument("--report", metavar="RESULTS_JSONL",
                    help="summarize a results file and exit")
    args = ap.parse_args()

    if args.report:
        report(args.report)
        return
    if not args.tasks or not args.repo:
        ap.error("--tasks and --repo are required unless --report")
    run(args)


if __name__ == "__main__":
    main()
