# Use cases — what to use DiffContext for, and how

Every recipe below uses only shipped, benchmarked behavior. Where a
recipe depends on a measured number, the number's source is linked.
Read the [fit check](#first-the-5-minute-fit-check) before adopting any
of them — DiffContext measurably does not fit every repo, and finding
that out takes 5 minutes.

## First: the 5-minute fit check

```bash
cd /path/to/your/repo
diffcontext verify --from-history 30 --calibrate
```

This mines real co-change cases from *your* git history, grades retrieval
against them, and reports mean recall, a precision lower bound, and
whether the sufficiency score tracks reality on your repo. Decision:

- **Mean recall well above 0.5, cases mostly pass** — the recipes below
  will work roughly as advertised. Add `--save-calibration` so later
  `verify` runs report a calibrated recall estimate instead of a bare
  score (fit on ≥30 of your own history cases).
- **NULL RESULT or recall near zero** — the tool does not fit this repo
  (common causes: CommonJS, heavy dynamic dispatch, non-Python/TS code —
  see the [language table](../README.md#language-support)). Stop here;
  none of the recipes below will be better than their baseline.

## 1. Context packing for a coding agent / LLM edit loop

The primary use case: before asking a model to modify code, hand it the
functions that historically change together with the target, instead of
grep results or whole files.

```python
from diffcontext.pipeline import index_repository, analyze_impact, compile

idx = index_repository(repo)                      # cached; warm ~0.02s
impact = analyze_impact(idx, changed_symbol_ids)  # hybrid ranking
ctx = compile(idx, impact, max_tokens=8000,
              token_counter=my_real_tokenizer)    # e.g. anthropic count_tokens
prompt = ctx.text            # includes the DROPPED manifest — the model
                             # is told what it cannot see
```

- In a long-running agent, use `idx.update(edited_files)` between turns
  (~0.5s) instead of re-indexing.
- If the loop is token-priced, pass `cutoff="gap"` — measured ~4× the
  precision of top-20 at 6–9 symbols for ~30% relative recall cost
  ([RIGOR_REPORT §7](../benchmarks/RIGOR_REPORT_2026-07.md)). Recall-first
  agents (a missed caller = a broken build) should keep the default.
- The equivalent one-shot CLI: `diffcontext compile --ref HEAD --json`.

Measured basis: ~2× grep's recall at every token budget, and grep
plateaus where this keeps climbing
([BENCHMARKS](BENCHMARKS.md#head-to-head-vs-grep-at-identical-token-budgets)).

## 2. "What breaks if I change this?" — pre-refactor and incident triage

```bash
diffcontext blast --changed ./src/auth.py:validate_jwt          # tree view
diffcontext blast --ref HEAD~1 --verify                         # with proof chains
```

`--verify` prints the concrete call-site evidence for every edge, so a
claimed impact can be checked rather than believed. Two caveats that are
measured, not theoretical: an empty blast radius for a symbol that
resolves dynamically (`getattr`-dispatch, metaclasses) means *statically
invisible*, not *safe*; and "no callers found" deserves a
`grep -rn "name("` spot-check before you rely on it
([known limitations](BENCHMARKS.md#known-limitations-measured-not-guessed)).

## 3. Reviewer context for a pull request

Give a human or LLM reviewer the co-change context of the diff — the
callers, callees, and siblings the diff's author saw, not just the diff:

```bash
git fetch origin main
diffcontext compile --ref origin/main --max-tokens 6000 --cutoff gap
```

The gap cutoff fits here: a reviewer wants the 6–9 most-implicated
symbols, and a miss is cheap (the diff itself is still in front of them).

## 4. CI gates

Two shipped gates, both exit-code driven:

**Context-sufficiency gate** — fail a pipeline when the compiled context
for a change is missing its direct dependencies:

```bash
diffcontext verify --ref "$BASE_SHA"     # exit 1 unless verdict SUFFICIENT
```

**Retrieval-expectation gate** — encode incidents as cases ("the PR #212
bug happened because the agent never saw `refresh_session`") and run them
on every push:

```bash
diffcontext verify --cases cases.json    # exit 1 if any case fails
```

Case format and what makes a good case: [VERIFY.md](VERIFY.md). This is
the same mechanism this repo uses to gate its own retrieval quality in CI
(`benchmarks/check_regression.py` is the heavyweight version).

## Choosing the operating point

| You are | Use | Because (measured) |
|---|---|---|
| An agent that must not break callers | default top-k | recall-first: hybrid recall 0.69–0.77 on benchmark repos |
| Paying per token / small context | `--cutoff gap` | ~4× precision at 6–9 symbols, −30% relative recall |
| Unsure | run both once | `verify --from-history 20 [--cutoff gap]` prints both operating points on your repo |

## What NOT to use it for

- **Non-Python/TS code, or CommonJS JS** — measured 0% on express; the
  index will be empty or wrong ([language table](../README.md#language-support)).
- **Semantic code search** ("where is rate limiting implemented?") — the
  query language is a *changed symbol or diff*, not a question. For
  conceptual queries unrelated to a change, use an embedding-based tool;
  dense retrieval is measurably the only signal that reaches
  cross-subsystem conceptual links ([RIGOR_REPORT §5](../benchmarks/RIGOR_REPORT_2026-07.md)).
- **Completeness-critical audits** (security reviews, "find every
  caller before deleting"). Recall is 0.69–0.77 on the benchmark repos,
  not 1.0, and the failure taxonomy lists what it systematically misses.
  Use it to find *most* of the impact fast, then verify the remainder
  with grep and tests.
