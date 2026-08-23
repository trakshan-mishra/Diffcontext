# ContextBench Evaluation — Session State (Aug 22, 2026; updated Aug 23)

## ⚠️ Lessons learned Aug 23 — READ BEFORE RUNNING ANYTHING

1. **The official evaluator (`contextbench.core.repo.checkout`) pollutes any
   local clone it is pointed at.** It runs
   `git fetch --depth 1 --filter=blob:none origin <commit>` on its `base_dir`
   (repo.py:75-80) unconditionally — even when the commit is already present —
   and this writes a `.git/shallow` file, converting a FULL clone into a
   shallow one. The pass@1 run's `ScratchClone` then does
   `git clone --local` from the now-shallow clone and
   `git checkout` fails with `fatal: unable to read tree <sha>`.
   **NEVER point the evaluator at `/home/trakshan/cb/*_canonical`.** Use a
   disposable copy (or let it clone fresh into its `--cache` dir). The shallow
   file is spurious (all objects were already present from the full clone);
   removing `.git/shallow` repairs the clone. This was the real root cause of
   the "concurrency corruption" — it was not concurrency, it was the evaluator
   making the canonical clone shallow.

2. **`run_diffcontext.py` was pointing at `benchmark_repos/` (SHALLOW clones)
   and used a relative worktree path git resolved against the repo dir.** Fixed
   Aug 23: it now uses `CANONICAL_CLONES` (full clones at `/home/trakshan/cb/`),
   makes the worktree path absolute, and has a `checkout()` postcondition
   (HEAD matches commit). The 136-task × 4-variant retrieval run now completes
   with 0 errors (`results/retrieval_136_4var/`).

3. **The `diffcontext_rerank` variant is NOT in HEAD.** Its source lives only
   in the `eval/rigor-pass` branch; `analyze_impact()` at HEAD takes no
   `rerank` kwarg and `compile()` has no `rerank`. Dropped from the retrieval
   run. The 57-task rerank number (F1 0.180, doesn't help) stays in RESULTS.md
   as the recorded result. Run 4 variants: seeds_only, retrieved_only,
   seeds_plus_retrieved, diffcontext_gap.

4. **Effective n for retrieval = 128** (8 django oracle misses have no
   function-level seeds). verify_results.py reports retrieval metrics over
   n=128 by default; the all-136 numbers (vacuous-precision inflation) are
   disclosed beneath. The pooled print in run_diffcontext.py uses n=136 — do
   not paste that as the headline.

5. **ContextBench evaluator repo:** `github.com/EuniAI/ContextBench` (NOT
   `github.com/contextbench/ContextBench`, which is a Next.js placeholder).
   Leaderboard: best Context F1 = 0.344 (Claude Sonnet 4.5); GLM-5.1 = 51.4%
   pass@1. Our diffcontext_gap line-F1 = 0.358 (retrieval, n=128) is in that
   ballpark — but note it's oracle-seeded retrieval F1, not agent-trajectory
   F1. Paper: arxiv.org/abs/2602.05892.

## Objective
Use ContextBench (external benchmark, 1,136 tasks with human-annotated gold
contexts) to evaluate DiffContext's context-retrieval quality and GLM 5.2's
downstream pass@1 on Python tasks, including a precision improvement
(`cutoff="gap"`).

## Working Directory
`/home/trakshan/temporary/titanic.csv/diff/Diffcontext`

## GLM Endpoint
- URL: `http://34.41.10.8:4000/v1`
- Key: stored in `.env` (gitignored, NOT rotated)
- Key name: `GLM_API_KEY`, `GLM_BASE_URL`
- Only 1 model visible: `glm-5.2` (LiteLLM may scope /models to key — other
  models may exist under a different key scope; not probed)

## Python Environments
- System Python 3.14: has diffcontext, rank_bm25, pytest, datasets,
  tree-sitter-language-pack, legacy-cgi
- Python 3.11 for Django tests: `/home/trakshan/temporary/cb_tmp/py311/bin/python`
  (has cgi, pkgutil.get_loader, django, pytz, pytest installed)

## Canonical Full Clones (non-shallow, gc.auto=0)
- `/home/trakshan/cb/django_canonical`
- `/home/trakshan/cb/requests_canonical`
- `/home/trakshan/cb/flask_canonical`
- Scratch clones at `/home/trakshan/cb/scratch/` via `git clone --local --no-hardlinks`

## ContextBench Dataset
- 1,136 tasks total, 512 Python across 20 repos
- Only `train` split exists (no test/validation)
- Two configs: `default` (1,136 tasks) and `contextbench_verified` (500 tasks)
- We use `default`, `split="train"`, revision `c2855792b006af41c67202d33883fb9d46362853`
- Only 136 Python tasks in our cloned repos: django (129), requests (6), flask (1)
- Cloned at `/tmp/opencode/ContextBench/` (for `python -m contextbench.evaluate`)
- Saved to `/tmp/opencode/cb_data/full.parquet` (26MB, 1,136 rows)

---

## SETTLED — do not re-test these

1. **Gate 1 passed.** 5 tasks end-to-end, zero git failures, setup 0.1-0.5s.
   Per-worker clone and `reset -f --detach` + `clean -fdqx` fixes hold.
   Postcondition assert in `reset_to` (HEAD matches commit, tree clean).

2. **Null content root cause and fix.** GLM 5.2 is a reasoning model.
   `reasoning_effort: "none"` does NOT suppress reasoning (param dropped by
   LiteLLM, 68-70k chars of reasoning_content either way).
   `chat_template_kwargs: {"enable_thinking": false}` DOES work:
   0 chars reasoning, 5-8s latency, content produced.
   Integrated into `run_glm_pass1.py`.
   `extra_body: {"thinking": {"type": "disabled"}}` also works (4.7s, 0c reasoning).
   `thinking: {"type": "disabled"}` partially works (40k vs 68k reasoning, not
   recommended).

3. **`bd3d7a3d` is an oracle miss.** Gold patch modifies `URLValidator.regex`,
   a class-level attribute. DiffContext extracts function-level symbols only.
   Same for all 8 oracle misses — all are class/module-level, none function-level.

4. **Seed census complete.** 128/136 tasks have >=1 function-level seed.
   8/129 django tasks are oracle misses (5.9% miss rate). 0 misses in
   requests (6 tasks) and flask (1 task). Effective n for retrieval = 128.

5. **Zero-seed tasks were INCLUDED (not dropped)** in the original 136-task run.
   Every variant including the oracle floor is depressed by tasks where no
   context selection could succeed. Effective n for retrieval evaluation = 128.

6. **requests 6-vs-7 settled.** The filter consistently produces requests=6.
   The "7" was never in any artifact — a miscounting from an earlier session.
   Per-repo denominator is now printed at startup.

7. **Dataset split confirmed.** Only `train` split exists. Contains all 1,136
   tasks (512 Python across 20 repos). We use `default` config correctly.

8. **All benchmark_repos clones were shallow.** Created full canonical clones
   for django, requests, flask at `/home/trakshan/cb/`. Updated
   `CANONICAL_CLONES` dict in `run_glm_pass1.py`.

9. **Earlier pass@1 results are CONTAMINATED** (v1 and v3 in
   `results/glm_pass1/`). v1 used worktrees (corruption), v3 reused one
   worktree across variants without `-f` flag. The v1 file also has duplicate
   rows. Mark as **unverified** in RESULTS.md, do not delete, do not defend.
   Superseded by the rerun.

10. **HF dataset revision pinned** to `c2855792b006af41c67202d33883fb9d46362853`
    in both `run_glm_pass1.py` and `run_diffcontext.py`.

---

## Ground Rules (MUST follow)

1. A diagnosis must be supported by an artifact **in the run you are
   reporting on**. Do not carry explanations forward from earlier sessions.
   If the current run lacks evidence, write "unknown".
2. `—` is not `0`. A field that was never measured is unmeasured, not zero.
3. Never call a fix confirmed unless the confirming run is in the artifacts,
   and state the n. A single success against a nondeterministic failure is
   not confirmation.
4. Separate infrastructure failures from model failures in every rate.
5. Do not re-propose a fix that has already been tried and failed.
6. Capture complete stderr, never a prefix.
7. No number enters a markdown file by hand — it comes from `verify_results.py`.

---

## error_class Taxonomy (in JSONL)

```
error_class ∈ {null, "setup_error", "no_seeds", "gen_error",
                "apply_error", "test_error", "skipped_no_llm"}
```

- `null` — success (passed=True or passed=False with test_error)
- `setup_error` — git/clone/patch infrastructure failure
- `no_seeds` — methodology limitation (oracle miss: gold patch touches
  class/module-level code, not functions)
- `gen_error` — LLM generation failure (null content, HTTP error)
- `apply_error` — patch application failure (model produced malformed diff)
- `test_error` — tests ran but did not pass
- `skipped_no_llm` — `--no-llm` smoke test (not an error)

Row cardinality: **one row per (instance_id, variant), always**. Matched
pairs required for paired statistics (sign test, bootstrap CI).

---

## JSONL Schema (current, in run_glm_pass1.py)

```json
{
  "instance_id": "...",
  "variant": "diffcontext|diffcontext_gap|none",
  "repo": "django|requests|flask",
  "diffcontext_sha": "bb5089855456",
  "model": "glm-5.2",
  "timestamp_utc": "...",
  "n_seeds": int,
  "ctx_tokens": int|null,
  "ctx_len": int|null,
  "f2p_tests": [...],
  "passed": bool|null,
  "test_exit_code": int|null,
  "error_class": str|null,
  "error_detail": str|null,
  "head_sha": str|null,
  "tree_status": str|null,
  "content_source": "content"|"reasoning"|null,
  "finish_reason": str|null,
  "reasoning_len": int|null,
  "completion_tokens": int|null,
  "diff_len": int (only if diff generated),
  "applied": bool (only if apply attempted),
  "sec": float
}
```

---

## Oracle Misses (8 tasks, all django, all class/module-level)

| ID | Category | Detail |
|----|----------|--------|
| bd3d7a3d | class-level attribute | URLValidator.regex |
| 1a760e52 | class-level attribute | ASCIIUsernameValidator.regex |
| 607f6b9e | class-level attribute | Avg/Sum.allow_distinct |
| 01ac491d | class-level method addition | Choices.__str__ (replaced `pass`) |
| 16db8cc6 | class-level data structure | operator dict in expressions.py |
| b2cb9f4b | module-level constant | dateparse.py regex patterns |
| 405a9ea4 | module-level constant | language_code_prefix_re |
| 16c72e4c | module-level variable | SECURE_REFERRER_POLICY |

Note: git `@@` context annotations were misleading for some of these
(e.g., 16db8cc6 showed `def __hash__` but the actual change was 40 lines
away in a class-level list comprehension). Verified by checking the actual
source at the changed line numbers.

---

## Completed Work

### Task 1: JSONL Classification Fix ✅
- `no_seeds` is its own bucket, separate from `setup_error`
- One row per (instance_id, variant) — validated with 272 rows, 0 duplicates
- Empty-task-list guard: exits non-zero with filter params if no tasks match
- `--no-llm` summary prints seed census table, not pass rate
- Per-repo denominator printed after filtering, before any slicing

### Task 2: Seed Extraction Census ✅
- 128/136 tasks with seeds (94.1%), 8 oracle misses (5.9%), 0 setup errors
- Per repo: django 129 (121 seeds, 8 miss, 6.2%), requests 6 (6 seeds),
  flask 1 (1 seed)
- All 8 misses are class-level or module-level (not function-level)
- Zero-seed tasks were INCLUDED in original run (not dropped)
- Effective n for retrieval evaluation = 128

### Task 3: Backbone Discriminating Tests ✅
- `reasoning_effort: "none"` — does NOT work (param dropped by LiteLLM)
- `chat_template_kwargs: {"enable_thinking": false}` — WORKS (0c reasoning,
  7.7s, content produced). Integrated into `run_glm_pass1.py`.
- `extra_body: {"thinking": {"type": "disabled"}}` — also works (4.7s, 0c)
- `thinking: {"type": "disabled"}` — partial (40k vs 68k reasoning)
- Evidence: `/tmp/glm_discriminating.log`

### Task 4: Salvage Parser ✅
- `_extract_diff_from_reasoning()` in `run_glm_pass1.py`
- Searches reasoning_content for fenced diff, unfenced `diff --git`, `--- a/`
- `content_source: "content" | "reasoning"` field in JSONL
- With B2 suppressing reasoning, salvage parser rarely fires — but labeled,
  never laundered as clean PASS

### Task 6: Contamination Flag ✅
- `reset_to` postcondition assert (HEAD matches, tree clean) — in place
- `head_sha`, `tree_status` logged per row — confirmed populated
- Old v1/v3 results to be marked **unverified** in RESULTS.md rewrite
- v1 also has duplicate rows (each (iid, variant) pair appears twice)

### GLM Pass@1 End-to-End Verification ✅
- 3-task test: 1 PASS (93721db4 diffcontext_gap), 2 apply_error, 1 no_seeds
- Latency: 2.6-8.7s (was 63-140s with reasoning on)
- `content_source: "content"`, `finish_reason: "stop"`, `reasoning_len: 0`
  on all generated rows
- First verified pass@1 result in this project
- Return-value bug in `glm_generate` (2-value vs 3-value return) — FIXED

### Infrastructure Fixes ✅
- Full canonical clones created for django, requests, flask
- HF dataset revision pinned in both scripts
- Empty-task-list guard with non-zero exit
- `--no-llm` prints seed census, not pass rate
- Per-repo denominator printed at startup

---

## Pending: Task 5 — Phase 4 (the actual study)

This is the main remaining task. Everything that carries the result runs
through `--no-llm` at ~0.3s/task — the full set is about a minute, free.

### 5a. Baselines (same budget, same harness, as variants)
| `whole_file` | every file containing a seed symbol, full contents |
| `import_closure` | seeds + 1-hop static imports, no relevance scoring |
| `bm25` | BM25 over symbols, query = `problem_statement`, no seeds |
| `random_k` | random symbols to budget (floor) |

Without these, "+42% recall over oracle seeds" has no reference point.

### 5b. Full 136-task run × 5 variants + 4 baselines
Retire the 57-task subset — it was "whatever finished before the crash",
order-dependent, not random.

### 5c. Official evaluator on all 136
`python -m contextbench.evaluate` on all 136 (not 7) at all four
granularities (file/symbol/span/line). In the 7-task eval, symbol coverage
rose 0.679 → 0.821 and file coverage 0.538 → 0.692 while line precision
collapsed. DiffContext retrieves *symbols*; line-level precision penalises
returning a whole function when gold marks four lines inside it. Lead with
symbol coverage, disclose line-level beneath it.

### 5d. Splits
- Per repo (129/136 are django — current results are a django result labelled
  as a DiffContext result)
- By patch shape: single-file vs multi-file gold patch, and by gold symbol
  count. Hypothesis: retrieval earns its keep when the fix is *not* local.

### 5e. Budget sweep
2k/4k/8k/16k/32k. At 4k the default variant reported `ctx_tok` 3928 and 3836
against a 4000 cap — it is saturating, so part of "gap uses fewer tokens" is
a cap artifact. The informative comparison is at budgets where neither
variant clips.

### 5f. Statistics
- Sign test on paired win/loss (`scipy.stats.binomtest`)
- Bootstrap CI on mean per-task F1 delta
- Characterisation of the losses — pattern or noise?

### After Task 5
- Write `benchmarks/contextbench/verify_results.py` (regenerates every
  number in RESULTS.md from JSONL, fails on mismatch)
- Rewrite RESULTS.md once, from `verify_results.py` output

---

## Relevant Files

### Scripts
- `benchmarks/contextbench/run_glm_pass1.py` — GLM pass@1 pilot (fully
  rewritten: ScratchClone, reset_to postcondition, --no-llm,
  checkpoint/resume, provenance, full stderr, new JSONL schema,
  chat_template_kwargs to suppress reasoning, salvage parser)
- `benchmarks/contextbench/run_diffcontext.py` — retrieval adapter (5
  variants: seeds_only, retrieved_only, seeds_plus_retrieved,
  diffcontext_gap, diffcontext_rerank). Still uses `Worktree` class — needs
  ScratchClone fix for Phase 4 rerun (same fix as run_glm_pass1.py)

### Results (stored artifacts)
- `benchmarks/contextbench/results/` — all stored artifacts
  - `retrieval_136/` — 3 variants, 136 tasks (old run, 0 errors)
  - `retrieval_57_gap/` — 5 variants, 136 tasks (79 git errors from
    corruption — superseded by canonical clone fix)
  - `official_eval_7/` — 7 tasks (superseded by full 136 eval)
  - `glm_pass1/` — CONTAMINATED pilot results (v1, v3) — mark unverified

### Census and test artifacts (in /tmp, may be cleared)
- `/tmp/census_nollm.jsonl` — 136-task seed census (272 rows, 2 variants)
- `/tmp/glm_discriminating.log` — backbone test results
- `/tmp/glm_1task_test.jsonl` — 3-task end-to-end verification (has 1 PASS)
- `/tmp/smoke_nollm_v2.jsonl` — 5-task smoke test (earlier)
- `/tmp/smoke_nollm.jsonl` — 5-task smoke test (earlier, pre-fix)

### Environment
- `.env` — `GLM_API_KEY` and `GLM_BASE_URL` (gitignored, NOT rotated)
- `/tmp/opencode/ContextBench/` — cloned ContextBench repo (for
  `python -m contextbench.evaluate`)
- `/tmp/opencode/cb_data/full.parquet` — ContextBench dataset (26MB)

### External
- `/home/trakshan/cb/django_canonical` — canonical full django clone
- `/home/trakshan/cb/requests_canonical` — canonical full requests clone
- `/home/trakshan/cb/flask_canonical` — canonical full flask clone
- `/home/trakshan/temporary/cb_tmp/py311/bin/python` — Python 3.11 for Django tests

---

## Next Move (resume here)

1. **Fix `run_diffcontext.py`** — replace `Worktree` class with
   `ScratchClone` (same pattern as `run_glm_pass1.py`). Add canonical clone
   paths for all 3 repos. Add the empty-task-list guard and per-repo
   denominator print. Pin HF dataset revision (already done).

2. **Add 4 baselines** to `run_diffcontext.py`:
   - `whole_file` — every file containing a seed symbol, full contents
   - `import_closure` — seeds + 1-hop static imports, no relevance scoring
   - `bm25` — BM25 over symbols, query = `problem_statement`, no seeds
   - `random_k` — random symbols to budget (floor)

3. **Run full 136 × 9** (5 variants + 4 baselines) with `--no-llm` (~1 min).

4. **Run official evaluator** on all 136 at all 4 granularities.

5. **Run splits** (per-repo, single-file vs multi-file, by gold symbol count).

6. **Run budget sweep** (2k/4k/8k/16k/32k).

7. **Compute statistics** (sign test, bootstrap CI, loss characterization).

8. **Write `verify_results.py`** — regenerates every number from JSONL.

9. **Rewrite RESULTS.md** — once, from `verify_results.py` output.

10. **Run GLM pass@1 at scale** — with `chat_template_kwargs` fix, latency is
    3-9s/task. Full 128 tasks × 2 variants ≈ 30-40 min. Use Python 3.11 for
    Django tests. Use `tee /tmp/glm_pass1_full.log` for output capture.
    Inner timeout 280s, outer shell timeout 290000ms.
