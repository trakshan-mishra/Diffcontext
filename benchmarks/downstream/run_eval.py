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
                   anthropic (default)  ANTHROPIC_API_KEY / `ant auth login` (SDK)
                   gemini               GEMINI_API_KEY / GOOGLE_API_KEY (REST)
                   groq                 GROQ_API_KEY (REST, OpenAI-compatible)
                   openrouter           OPENROUTER_API_KEY (REST, OpenAI-compatible)
                 gemini/groq/openrouter need only `requests` — no vendor SDK.

Before scoring any provider on a task, a real run re-verifies that the
gold patch makes the tests pass IN THIS ENVIRONMENT (not just per stale
tasks.json): dep/pytest drift can render a once-valid task un-judgeable,
and such a task is skipped and logged to <repo>.skipped.jsonl rather than
polluting every arm with all-fail noise. Disable with --skip-gold-gate.

--sensitivity-gate additionally screens each task with the no-context arm and
keeps only the tasks it FAILS. A task the model already solves blind cannot
discriminate retrieval arms — every arm passes it — so including it only
dilutes the comparison toward the ceiling. The screen spends the `none` arm
(logged to <repo>.screen.jsonl, excluded from scoring since conditioning on it
makes it 0-by-construction) and leaves an unconfounded head-to-head between the
real retrieval arms. --report prints how many tasks actually separate the arms.

Results append to benchmarks/downstream/results/<repo>.jsonl (resumable:
already-recorded (task, provider, sample) rows are skipped). Mock self-
tests write to <repo>.mock.gold.jsonl / <repo>.mock.empty.jsonl. Summarize
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
DEFAULT_MODELS = {"anthropic": "claude-opus-4-8", "gemini": "gemini-flash-latest",
                  "groq": "llama-3.3-70b-versatile", "openrouter": "deepseek/deepseek-chat",
                  # Devstral 2 ids (GET /v1/models): devstral-2512 (small, open),
                  # devstral-medium-latest (123B). Override with --model.
                  "mistral": "devstral-2512"}
# OpenAI-compatible REST gateways (chat/completions). The backend name selects
# the base URL and the env var holding its key.
OPENAI_COMPAT = {
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1",   "OPENROUTER_API_KEY"),
    "mistral":    ("https://api.mistral.ai/v1",      "MISTRAL_API_KEY"),
}
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_CONTEXT_TOKENS = 8000
MAX_OUTPUT_TOKENS = 16000
HTTP_TIMEOUT = 180          # seconds per generation request
MAX_RETRIES = 5            # transient 429 (per-minute rate limit) backoff attempts

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

    Returns {'patch', 'stop_reason', 'usage', 'error'}. Every backend receives
    identical prompt text (assembled by `_prompt_parts`); only the wire format
    differs. Gemini and the OpenAI-compatible gateways (Groq, OpenRouter) go
    over plain HTTP via `requests`, so no vendor SDK is required.
    """
    if backend == "gemini":
        return _generate_gemini(client, model, task, seed_sources, context, test_diff)
    if backend in OPENAI_COMPAT:
        return _generate_openai_compatible(client, model, task, seed_sources, context, test_diff)
    return _generate_anthropic(
        client, model, build_messages(task, seed_sources, context, test_diff))


def _http_post_json(url: str, headers: dict, body: dict) -> tuple:
    """POST JSON with retry/backoff on transient failures. Returns (json, None)
    on success or (None, error_tag). Retried: network errors, a per-minute 429
    (honoring a server-sent retryDelay when present), and transient 5xx
    (500/502/503/504 — the provider hiccuping, seen in practice as sporadic
    503s). A hard per-day quota is NOT retried — waiting minutes can't help —
    and surfaces as `http_429:per_day_quota`. The point of all this: a rate cap
    or a server blip must never be silently recorded as a failed fix, which
    would corrupt the paired provider comparison.
    """
    import requests
    last = "exhausted"
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last = f"network:{type(e).__name__}"
            if attempt < MAX_RETRIES:
                time.sleep(min(5 * 2 ** attempt, 60))
                continue
            return None, last
        if r.status_code == 200:
            try:
                return r.json(), None
            except ValueError:
                return None, "bad_json"
        text = r.text or ""
        hard_daily = "PerDay" in text or "per day" in text.lower()
        transient = (r.status_code in (500, 502, 503, 504)
                     or (r.status_code == 429 and not hard_daily))
        if transient and attempt < MAX_RETRIES:
            # honor a server-suggested delay in either dialect: Gemini
            # "retryDelay: 6s", Groq/OpenAI "Please try again in 6.23s".
            m = re.search(r"(?:retryDelay|retry in|try again in)['\":\s]*([\d.]+)",
                          text, re.IGNORECASE)
            delay = min(int(float(m.group(1))) + 1, 60) if m else min(5 * 2 ** attempt, 60)
            time.sleep(delay)
            continue
        return None, f"http_{r.status_code}" + (":per_day_quota" if hard_daily else "")
    return None, last


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


def _generate_gemini(api_key: str, model: str, task: Task,
                     seed_sources: Dict[str, str], context: str,
                     test_diff: str) -> dict:
    """Gemini generation over the REST generateContent endpoint (no SDK).

    The prompt is one user text turn (SYSTEM_PROMPT goes in system_instruction),
    byte-identical across arms except the provider context block. Gemini's own
    context caching is not used: the eval's correctness doesn't depend on it,
    and skipping it keeps the per-arm payloads simple and identical. Thinking
    models (e.g. gemini-flash) spend `thoughtsTokenCount`; that's recorded but
    the thinking parts are dropped when reading the answer text.
    """
    fixed, context_block = _prompt_parts(task, seed_sources, context, test_diff)
    prompt = fixed + context_block + FINAL_INSTRUCTION
    gen_config = {"maxOutputTokens": MAX_OUTPUT_TOKENS}
    # 2.5-family flash models "think" by default (~10k+ tokens/call): slow, and
    # the reasoning can starve the answer of output budget. This eval doesn't
    # need the trace, so disable it where the model allows (pro can't set 0).
    if "2.5" in model and "pro" not in model:
        gen_config["thinkingConfig"] = {"thinkingBudget": 0}
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }
    j, err = _http_post_json(
        f"{GEMINI_API_BASE}/models/{model}:generateContent",
        {"Content-Type": "application/json", "X-goog-api-key": api_key}, body)
    if err:
        return {"patch": None, "stop_reason": "error",
                "error": f"api_error:{err}", "usage": {}}

    um = j.get("usageMetadata") or {}
    usage = {"input_tokens": um.get("promptTokenCount"),
             "output_tokens": um.get("candidatesTokenCount"),
             "thinking_tokens": um.get("thoughtsTokenCount"),
             "total_tokens": um.get("totalTokenCount")}

    fb = j.get("promptFeedback") or {}
    if fb.get("blockReason"):
        return {"patch": None, "stop_reason": f"blocked:{fb['blockReason']}",
                "error": "refused", "usage": usage}

    cands = j.get("candidates") or []
    if not cands:
        return {"patch": None, "stop_reason": "no_candidate",
                "error": "no_candidate", "usage": usage}
    cand = cands[0]
    stop = str(cand.get("finishReason", "?"))
    parts = ((cand.get("content") or {}).get("parts")) or []
    # keep only answer text; drop thinking parts (marked thought=True)
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    diff = extract_diff(text)
    return {"patch": diff, "stop_reason": stop,
            "error": None if diff else "no_diff_in_output", "usage": usage}


def _generate_openai_compatible(cfg: tuple, model: str, task: Task,
                                seed_sources: Dict[str, str], context: str,
                                test_diff: str) -> dict:
    """Generation over an OpenAI-compatible chat/completions endpoint (no SDK).

    `cfg` is (base_url, api_key). Groq and OpenRouter both speak this wire
    format, so one code path reaches any of their listed models (Llama, Kimi,
    DeepSeek, Qwen, ...). SYSTEM_PROMPT is the system turn; the task material +
    context is one user turn, byte-identical across arms except the context.
    """
    base_url, api_key = cfg
    fixed, context_block = _prompt_parts(task, seed_sources, context, test_diff)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": fixed + context_block + FINAL_INSTRUCTION},
        ],
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    j, err = _http_post_json(
        f"{base_url}/chat/completions",
        {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, body)
    if err:
        return {"patch": None, "stop_reason": "error",
                "error": f"api_error:{err}", "usage": {}}

    choices = j.get("choices") or []
    if not choices:
        return {"patch": None, "stop_reason": "no_candidate",
                "error": "no_candidate", "usage": {}}
    choice = choices[0]
    text = (choice.get("message") or {}).get("content") or ""
    finish = str(choice.get("finish_reason", "?"))
    u = j.get("usage") or {}
    usage = {"input_tokens": u.get("prompt_tokens"),
             "output_tokens": u.get("completion_tokens"),
             "total_tokens": u.get("total_tokens")}
    diff = extract_diff(text)
    return {"patch": diff, "stop_reason": finish,
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


def is_transient_error(row: dict) -> bool:
    """True if the row is an infrastructure failure (rate cap, 5xx, network),
    NOT a real measurement. Such rows are excluded from the resume set (so they
    get retried) and from --report (so a server blip never counts as a failed
    fix). `no_diff_in_output` and `refused` are genuine model outcomes and are
    NOT transient — they stay.
    """
    return str(row.get("gen_error") or "").startswith("api_error")


# ---------------------------------------------------------------------------
# Sensitivity gate — keep only tasks that can actually discriminate arms
# ---------------------------------------------------------------------------
# A task is informative only if the model FAILS it without context. If the
# no-context arm already passes, the task says nothing about retrieval: every
# arm passes and the comparison is diluted toward the ceiling. Screening those
# out is what turns a null sweep into a powered one.
#
# Selection effect, stated plainly: conditioning on `none` failing makes the
# `none` arm 0-by-construction on the retained set, so it is NOT a usable
# baseline there. The gate therefore drops `none` from the measured arms and
# writes its screening verdict to a separate <repo>.screen.jsonl. What remains
# is an unconfounded head-to-head between the real retrieval arms (neither was
# used for selection) — which is the comparison the eval exists to make.
SCREEN_PROVIDER = "none"


def load_screen(path: str) -> Dict[str, bool]:
    """Prior screening verdicts -> {commit: passed_without_context}. Transient
    rows are ignored so a 429 during screening is retried, never cached as a
    verdict."""
    verdicts: Dict[str, bool] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    r = json.loads(ln)
                    if not is_transient_error(r):
                        verdicts[r["commit"]] = bool(r["passed"])
    return verdicts


def run(args) -> None:
    global MAX_OUTPUT_TOKENS
    MAX_OUTPUT_TOKENS = args.max_output_tokens   # backends read this module global
    tasks = load_tasks(args.tasks)
    repo = os.path.abspath(args.repo)
    providers = args.providers.split(",")
    for p in providers:
        if p not in PROVIDERS:
            sys.exit(f"unknown provider {p!r}; known: {PROVIDERS}")

    # The gate spends the `none` arm as the screen, so it stops being a measured
    # arm (it would be 0-by-construction on the retained set — see SCREEN_PROVIDER).
    context_providers = list(providers)
    if args.sensitivity_gate:
        if SCREEN_PROVIDER not in providers:
            sys.exit(f"--sensitivity-gate screens with the {SCREEN_PROVIDER!r} arm; "
                     f"add it to --providers")
        providers = [p for p in providers if p != SCREEN_PROVIDER]
        if len(providers) < 2:
            sys.exit("--sensitivity-gate leaves fewer than 2 measured arms; "
                     f"pass at least two providers besides {SCREEN_PROVIDER!r}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    # --tag separates result files per model run. Resume is keyed by
    # (commit, provider, sample) with no model field, so two models sharing one
    # file would make the second skip every row as "already done" and the report
    # would mix models across arms — a tag per model keeps each run self-contained.
    # The same argument splits the gold and empty self-tests into SEPARATE files:
    # with a shared `.mock.jsonl` an empty run would skip rows a gold run already
    # wrote, silently blending the two.
    tag = f".{args.tag}" if args.tag else ""
    suffix = f".mock.{args.mock}.jsonl" if args.mock else ".jsonl"
    out_path = os.path.join(RESULTS_DIR, os.path.basename(repo) + tag + suffix)
    # un-judgeable tasks (gold patch fails in this env) are logged here, kept
    # out of the results file so they never enter the provider comparison.
    skip_path = out_path[:-len(".jsonl")] + ".skipped.jsonl"
    # Screening verdicts live outside the results file so the degenerate `none`
    # rows can never be mistaken for a baseline arm, and so a resumed run does
    # not re-pay for a screen it already made.
    screen_path = out_path[:-len(".jsonl")] + ".screen.jsonl"
    screened = load_screen(screen_path) if args.sensitivity_gate else {}
    # Resume set: a (commit, provider, sample) counts as done only if it's a
    # real measurement. Rows left behind by a transient infra failure (429/5xx/
    # network) are NOT counted, so a re-run retries them instead of baking in a
    # false "fail".
    done = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            for ln in f:
                if not ln.strip():
                    continue
                r = json.loads(ln)
                if not is_transient_error(r):
                    done.add(result_key(r))

    if args.model is None:
        args.model = DEFAULT_MODELS[args.backend]

    # `client` is whatever the backend's generate function needs: the Anthropic
    # SDK object, the Gemini API key string, or an (base_url, api_key) tuple for
    # the OpenAI-compatible REST gateways.
    client = None
    if not args.mock:
        if args.backend == "anthropic":
            import anthropic
            client = anthropic.Anthropic()  # ANTHROPIC_API_KEY or ant auth profile
        elif args.backend == "gemini":
            client = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not client:
                sys.exit("gemini backend needs GEMINI_API_KEY or GOOGLE_API_KEY "
                         "in the environment")
        else:  # groq / openrouter / mistral — OpenAI-compatible REST gateways
            base_url, key_env = OPENAI_COMPAT[args.backend]
            # OPENAI_BASE_URL points any of these backends at another
            # OpenAI-compatible free tier (Cerebras, a local vLLM, ...) without a
            # code change; OPENAI_API_KEY is the generic key fallback.
            base_url = os.environ.get("OPENAI_BASE_URL", base_url)
            api_key = os.environ.get(key_env) or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                sys.exit(f"{args.backend} backend needs {key_env} or OPENAI_API_KEY "
                         f"in the environment (set OPENAI_BASE_URL to target another "
                         f"OpenAI-compatible gateway)")
            client = (base_url, api_key)

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
                    for p in context_providers
                }
            finally:
                wt.remove()

            # Gold-validity gate (real runs only): trust nothing stale in
            # tasks.json — re-verify that the gold patch makes the tests pass
            # in THIS environment before scoring any provider on the task.
            # Dependency/pytest drift can render a once-valid task un-judgeable
            # (e.g. a strict `filterwarnings=error` config promoting a new
            # deprecation to a collection error), and scoring providers on such
            # a task adds only all-fail noise to every arm. Cheap (no API cost).
            if not args.mock and not args.skip_gold_gate:
                gv = apply_and_test(repo, task, task.gold_patch, scratch)
                if not gv["passed"]:
                    with open(skip_path, "a", encoding="utf-8") as sk:
                        sk.write(json.dumps({
                            "commit": task.commit, "repo": task.repo,
                            "skipped": "gold_fails_in_env",
                            "detail": gv.get("detail", ""),
                            "apply_strategy": gv.get("apply_strategy"),
                            "ts": time.time()}) + "\n")
                    print(f"[{ti}] {task.commit[:10]} SKIP: gold patch fails in "
                          f"this env (un-judgeable) -> {os.path.basename(skip_path)}")
                    continue

            # Sensitivity gate: does this task need context at all?
            if args.sensitivity_gate:
                solved_blind = screened.get(task.commit)
                if solved_blind is None:
                    srow = {"commit": task.commit, "repo": task.repo,
                            "provider": SCREEN_PROVIDER, "sample": 0,
                            "screen": True,
                            "backend": None if args.mock else args.backend,
                            "model": None if args.mock else args.model,
                            "context_tokens_budget": args.context_tokens,
                            "n_seeds": len(seeds), "ts": time.time()}
                    if args.mock:
                        # Self-test: --mock gold must screen EVERY task out (the
                        # patch passes without context), --mock empty must retain
                        # every task. Either outcome failing means the gate is broken.
                        screen_patch = task.gold_patch if args.mock == "gold" else None
                        srow["mock"] = args.mock
                    else:
                        gen = generate_patch(args.backend, client, args.model, task,
                                             seed_sources, contexts[SCREEN_PROVIDER],
                                             test_diff)
                        screen_patch = gen["patch"]
                        srow.update({"stop_reason": gen["stop_reason"],
                                     "gen_error": gen["error"], "usage": gen["usage"],
                                     "patch": (gen["patch"] or "")[:4000]})
                        if args.sleep:
                            time.sleep(args.sleep)
                    if is_transient_error(srow):
                        # Unknown, not "informative" — leave the task unscreened
                        # so a re-run retries it rather than admitting it blind.
                        with open(screen_path, "a", encoding="utf-8") as sc:
                            sc.write(json.dumps(srow) + "\n")
                        print(f"[{ti}] {task.commit[:10]} screen  "
                              f"{srow['gen_error']} — retry later")
                        continue
                    srow.update(apply_and_test(repo, task, screen_patch, scratch))
                    with open(screen_path, "a", encoding="utf-8") as sc:
                        sc.write(json.dumps(srow) + "\n")
                    solved_blind = bool(srow["passed"])
                if solved_blind:
                    print(f"[{ti}] {task.commit[:10]} SKIP: solved without context "
                          f"(uninformative) -> {os.path.basename(screen_path)}")
                    continue

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
                        # Proactive throttle: pause before each call to stay under
                        # free-tier per-minute rate limits, so 429 backoff stays a
                        # rare fallback rather than the common path.
                        if args.sleep:
                            time.sleep(args.sleep)
                        gen = generate_patch(
                            args.backend, client, args.model, task,
                            seed_sources, contexts[provider], test_diff)
                        patch = gen["patch"]
                        row.update({"stop_reason": gen["stop_reason"],
                                    "gen_error": gen["error"],
                                    "usage": gen["usage"],
                                    "patch": (gen["patch"] or "")[:4000]})
                        if args.sleep:            # free-tier per-minute throttle
                            time.sleep(args.sleep)

                    verdict = apply_and_test(repo, task, patch, scratch)
                    row.update(verdict)
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    mark = "PASS" if verdict["passed"] else "fail"
                    print(f"[{ti}] {task.commit[:10]} {provider:16s} s{sample} {mark}")


# ---------------------------------------------------------------------------
# Reporting — paired per-task comparison, Wilcoxon + Holm
# ---------------------------------------------------------------------------

def is_measurement(row: dict) -> bool:
    """True for a scoreable arm result. Excludes the two sidecar row shapes that
    share the results directory and get swept up by a `results/*.jsonl` glob:
    gold-gate skips (no `provider` at all) and sensitivity-gate screens (which
    DO carry provider='none' and would otherwise be silently counted as a
    baseline arm — the exact confound the gate exists to avoid)."""
    return "provider" in row and not row.get("screen")


def _load_measurements(path: str) -> tuple:
    """Load one results file → (rows, n_transient). Dedupe to one measurement
    per (commit, provider, sample) keeping the LAST (a retried task appends a
    fresh row), and drop transient infra failures so a 429/5xx never counts as
    a failed fix."""
    with open(path, encoding="utf-8") as f:
        raw = [json.loads(ln) for ln in f if ln.strip()]
    dedup: Dict[tuple, dict] = {}
    n_transient = 0
    for r in raw:
        if not is_measurement(r):
            continue
        if is_transient_error(r):
            n_transient += 1
            continue
        dedup[result_key(r)] = r
    return list(dedup.values()), n_transient


def _rank_biserial(x: List[float], y: List[float]) -> float:
    """Matched-pairs rank-biserial effect size for the paired Wilcoxon: in
    [-1, 1], positive when x tends to exceed y. r = (W+ - W-) / (W+ + W-),
    ranking |differences| with average ranks for ties. 0 when all pairs tie."""
    diffs = [a - b for a, b in zip(x, y) if a != b]
    n = len(diffs)
    if n == 0:
        return 0.0
    order = sorted(range(n), key=lambda i: abs(diffs[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(diffs[order[j + 1]]) == abs(diffs[order[i]]):
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_plus = sum(r for r, d in zip(ranks, diffs) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, diffs) if d < 0)
    total = w_plus + w_minus
    return (w_plus - w_minus) / total if total else 0.0


def _report_rows(rows: List[dict], label: str, n_transient: int = 0) -> None:
    """Print pass rates + paired Wilcoxon (Holm-corrected) with paired delta and
    rank-biserial effect size, for one already-loaded, deduped row set."""
    print(f"===== {label} =====")
    if n_transient:
        print(f"note: dropped {n_transient} transient-error row(s) "
              f"(429/5xx/network — re-run to retry them)")
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

    # Discrimination diagnostic. A paired test can only see tasks where the arms
    # actually disagree: tasks every arm passes (ceiling) or every arm fails
    # (floor) contribute a zero difference to every pair and carry no signal. If
    # informative == 0 the null is a property of the TASK SET, not evidence that
    # the arms are equivalent — the fix is task selection (--sensitivity-gate
    # removes the ceiling half), not more samples.
    if common and len(providers) >= 2:
        ceiling = floor = 0
        for c in common:
            vals = [sum(by_pc[(p, c)]) / len(by_pc[(p, c)]) for p in providers]
            if all(v == 1.0 for v in vals):
                ceiling += 1
            elif all(v == 0.0 for v in vals):
                floor += 1
        informative = len(common) - ceiling - floor
        print(f"\ndiscrimination: {informative}/{len(common)} tasks separate the arms "
              f"({ceiling} solved by all = ceiling, {floor} solved by none = floor)")
        if not informative:
            print("  !! no task distinguishes any arm — this result set cannot "
                  "support ANY claim about retrieval, in either direction")

    if len(common) >= 6 and len(providers) >= 2:
        print("\nPaired Wilcoxon (two-sided) vs. top arm, Holm-corrected; "
              "delta = top−arm pass rate, rbc = rank-biserial effect size:")
        ordered = sorted(providers, key=lambda p: -means[p])
        primary = ordered[0]
        x = [sum(by_pc[(primary, c)]) / len(by_pc[(primary, c)]) for c in common]
        rows_out, ps = [], []
        for p in ordered[1:]:
            y = [sum(by_pc[(p, c)]) / len(by_pc[(p, c)]) for c in common]
            _, pval, n_eff = wilcoxon_signed_rank(x, y)
            rows_out.append((p, means[primary] - means[p], _rank_biserial(x, y), pval, n_eff))
            ps.append(pval)
        adj = holm_bonferroni(ps)
        for (b, delta, rbc, pval, n_eff), ap in zip(rows_out, adj):
            print(f"  {primary} vs {b:16s} delta={delta:+.3f}  rbc={rbc:+.3f}  "
                  f"p={pval:.4f}  holm={ap:.4f}  (n_eff={n_eff})")
    else:
        print("\n(too few complete tasks for a paired test — need >= 6)")
    print()


def report(paths: List[str]) -> None:
    """Summarize one or more results files. With several files, prints a
    per-file section and then a POOLED section over all of them (commits are
    distinct SHAs across repos, so pooling simply widens the paired sample —
    the fix for the per-repo power problem)."""
    all_rows: List[dict] = []
    any_rows = False
    for path in paths:
        rows, n_transient = _load_measurements(path)
        if not rows:
            print(f"===== {os.path.basename(path)} =====")
            print(f"no valid rows ({n_transient} transient-error rows dropped)\n")
            continue
        any_rows = True
        _report_rows(rows, os.path.basename(path), n_transient)
        all_rows.extend(rows)
    if not any_rows:
        sys.exit("no valid rows in any file")
    if len(paths) > 1 and all_rows:
        _report_rows(all_rows, f"POOLED ({len(paths)} files)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", help="tasks JSON from tasks.py")
    ap.add_argument("--repo", help="path to the benchmark repo clone")
    ap.add_argument("--providers", default=",".join(PROVIDERS))
    ap.add_argument("--backend",
                    choices=["anthropic", "gemini", "groq", "openrouter", "mistral"],
                    default="anthropic",
                    help="LLM provider for generation (mock modes ignore this). "
                         "gemini/groq/openrouter/mistral go over REST (requests only, "
                         "no SDK)")
    ap.add_argument("--model", default=None,
                    help=f"model id; defaults per --backend: {DEFAULT_MODELS}")
    ap.add_argument("--samples", type=int, default=1,
                    help="generations per (task, provider)")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to pause before each LLM generation, to stay "
                         "under free-tier per-minute rate limits (0 = no throttle)")
    ap.add_argument("--tag", default=None,
                    help="suffix for the results filename (e.g. the model name), so "
                         "each model writes its own resumable file: "
                         "<repo>.<tag>.jsonl. Keep one tag per model — mixing "
                         "models in one file confounds the paired report.")
    ap.add_argument("--context-tokens", type=int, default=DEFAULT_CONTEXT_TOKENS)
    ap.add_argument("--max-output-tokens", type=int, default=MAX_OUTPUT_TOKENS,
                    help=f"cap on generated tokens (default {MAX_OUTPUT_TOKENS}). "
                         "Lower it for providers that count max_tokens against a "
                         "per-minute budget — Groq free tier rejects a request whose "
                         "input+max_tokens exceeds its 12k TPM cap")
    ap.add_argument("--mock", choices=["gold", "empty"],
                    help="harness self-test instead of LLM calls")
    ap.add_argument("--skip-gold-gate", action="store_true",
                    help="do NOT re-verify gold->pass per task before scoring "
                         "(by default un-judgeable tasks are skipped and logged "
                         "to <repo>.skipped.jsonl)")
    ap.add_argument("--sensitivity-gate", action="store_true",
                    help="screen each task with the 'none' arm first and keep only "
                         "the tasks it FAILS (a task the model solves blind cannot "
                         "discriminate retrieval arms). Spends 'none' as the screen, "
                         "logging it to <repo>.screen.jsonl, and measures the "
                         "remaining arms head-to-head")
    ap.add_argument("--scratch", default=os.environ.get("TMPDIR", "/tmp"))
    ap.add_argument("--report", metavar="RESULTS_JSONL", nargs="+",
                    help="summarize one or more results files and exit; with "
                         "several files also prints a POOLED cross-repo analysis")
    args = ap.parse_args()

    if args.report:
        report(args.report)
        return
    if not args.tasks or not args.repo:
        ap.error("--tasks and --repo are required unless --report")
    run(args)


if __name__ == "__main__":
    main()
