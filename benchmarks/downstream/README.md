# Downstream (rung-5) eval — does better context improve LLM patch outcomes?

Every number elsewhere in `benchmarks/` is proxy retrieval quality against
co-change ground truth. This harness measures the claim that actually
matters: **with the model, prompt, token budget, and localization held
fixed, does changing only the context provider change whether the LLM's
patch makes the repo's own tests pass?**

## Design

```
tasks.py    mine commits that changed code AND tests; task state = parent
            code + the commit's tests; keep only tasks where the tests
            machine-verifiably FAIL at the state and PASS at the commit
run_eval.py for each task x provider: compile context at the task state,
            ask the model for a unified diff, apply it, run the tests
--report    per-provider pass rates + paired Wilcoxon (Holm-corrected)
            over tasks common to all providers (benchmarks/significance.py)
```

Held fixed across arms: model (`claude-opus-4-8` by default), system
prompt, user prompt (test diff, failing output, seed-function sources),
context token budget, seeds. The **only** varying bytes are the provider's
context block, which is deliberately placed last so the per-task prefix is
byte-identical across arms and served from the prompt cache.

Providers: `diffcontext` (hybrid top-k), `diffcontext_gap` (precision
cutoff), `bm25`, `samefile`, `none`.

## Harness validation (run before spending API budget)

```bash
# 1. Mine + machine-validate tasks (no LLM, no key):
python benchmarks/downstream/tasks.py benchmark_repos/click --target 20

# 2. Self-test the judge — gold patches must ALL pass, empty must ALL fail:
python benchmarks/downstream/run_eval.py --tasks benchmarks/downstream/tasks/click.json \
    --repo benchmark_repos/click --mock gold
python benchmarks/downstream/run_eval.py --tasks benchmarks/downstream/tasks/click.json \
    --repo benchmark_repos/click --mock empty --providers none
```

Verified 2026-07-20 on click: 5/6 candidate commits validated into tasks;
gold mock 25/25 PASS across all five providers; empty mock 0/5.

## The real run

Pick a backend with `--backend`. The generation model is held FIXED across
every arm, so the only thing that varies is the provider context block.

```bash
# --- Anthropic (default) ---
pip install anthropic                      # not a package dependency
export ANTHROPIC_API_KEY=...               # or `ant auth login`
python benchmarks/downstream/run_eval.py \
    --tasks benchmarks/downstream/tasks/click.json \
    --repo benchmark_repos/click --samples 3

# --- Gemini ---
pip install google-genai                   # not a package dependency
export GEMINI_API_KEY=...                  # or GOOGLE_API_KEY
python benchmarks/downstream/run_eval.py \
    --tasks benchmarks/downstream/tasks/click.json \
    --repo benchmark_repos/click --backend gemini --samples 3

python benchmarks/downstream/run_eval.py --report benchmarks/downstream/results/click.jsonl
```

`--model` overrides the per-backend default (anthropic → `claude-opus-4-8`,
gemini → `gemini-2.5-pro`). The runner reads the API key from the
environment only — never pass a key on the command line or commit one.

Results append to a JSONL and runs are resumable. Cost: ~$0.10 per
generation on opus-4-8 before cache savings; 20 tasks x 5 providers x
3 samples ≈ $30 ceiling, substantially less with the cache.

## Disclosed limitations (read before citing any number)

- **Oracle localization.** Providers are seeded with the gold-changed
  symbols. The measured claim is therefore "given correct localization,
  does context quality matter?" — NOT end-to-end issue solving. This
  isolates the variable DiffContext actually controls; it also means the
  numbers are not comparable to SWE-bench leaderboard figures.
- **Contamination.** The benchmark repos are public and the gold fixes are
  in the model's training data. The `none` arm is the probe: its pass rate
  is the memorization + seed-source floor, and only the *paired deltas*
  between arms carry the context-quality signal (memorization applies
  equally to every arm — same model, same task). If `none` saturates,
  the task set cannot discriminate and needs post-cutoff commits.
- **Task family.** Test-verified fix tasks skew toward well-tested,
  localized changes; cross-subsystem changes rarely produce a clean
  fail@state/pass@gold pair. This under-samples exactly the bucket where
  retrieval differences might matter most — the direction of that bias is
  AGAINST finding an effect, which is the acceptable direction.
- **Power.** With ~20 tasks per repo, only large effects reach p<0.05 in a
  paired test. Pool across repos (run per-repo, report per-repo AND
  pooled) before claiming a null; report effect sizes alongside p-values.
- **Stochasticity.** Sampling parameters are not configurable on
  opus-4-8; use `--samples 3` and per-task mean pass so per-generation
  noise averages out.
