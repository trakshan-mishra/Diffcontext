# ContextBench — technical execution & implementation plan

**Question this benchmark exists to answer, stated so it can be falsified:**
with the generation model, prompt, token budget, and code localization held
fixed, does **changing only the context provider** change whether an LLM's
patch makes a repository's own tests pass?

- **Null hypothesis (H0):** for each provider pair, the paired per-task pass
  rate is equal. A result that fails to reject H0 is a *publishable* finding —
  it says "for this task family, retrieval quality does not move downstream
  patch success," and it is reported as such, not re-run until it flips.
- **Scope:** this is the rung-5 *downstream* claim. Every other number in
  `benchmarks/` is proxy retrieval quality against co-change ground truth; this
  is the only harness that measures the outcome that actually matters.

This document is the order of operations. It is grounded strictly in what is
measured in this repo today; every results table below is **blank by design**
and is filled only from `--report` output over recorded `.jsonl` rows (the
downstream `results/` dir is gitignored; raw rows live there). See §11.

---

## 1. Status snapshot (what is real today)

| Component | State | Evidence |
|---|---|---|
| Retrieval quality (proxy, upstream) | **Measured** | LORO held-out mean hit 0.855 / recall 0.689 / precision 0.072 (`results/loro/loro_3leg.json`) |
| Sufficiency-score calibration | **Measured** | evidence-aware r=0.287 Python n=1080; learned-weight transfer null (`RIGOR_REPORT_2026-07.md`) |
| Downstream harness (`run_eval.py`) | **Built + validated** | REST backends, gold gate, transient-safe; mock gold/empty self-tests pass |
| Generation model for free-tier sweep | **Working** | `gemini-flash-latest` (→ gemini-3.5-flash) emits applying diffs, 9/9 on smoke set |
| Downstream **results** (the RQ above) | **NOT YET RUN at scale** | tables in §8 are blank |

**Task inventory (validated, gold→PASS / fail@state machine-checked):**

| repo | tasks | | repo | tasks |
|---|---|---|---|---|
| click | 15 | | starlette | 12 |
| rich | 12 | | flask | 11 |
| requests | 4 | | httpx | 0 *(needs mining)* |

Total ≈ 54 tasks across 5 repos. **Providers (arms):**
`diffcontext` (hybrid top-k), `diffcontext_gap` (precision cutoff), `bm25`,
`samefile`, `none`.

---

## 2. Design — the controlled experiment

```
tasks.py     mine commits that changed BOTH code and tests; task state =
             parent code + the commit's test files; keep only tasks that
             machine-verifiably FAIL at the state and PASS at the gold commit.

run_eval.py  for each (task × provider): index the repo AT THE TASK STATE,
             compile the provider's context for the gold-changed seed symbols,
             ask the fixed model for a unified diff, apply it, run the tests.

--report     dedupe → per-provider pass rate → paired Wilcoxon, Holm-corrected,
             over tasks common to every arm (benchmarks/significance.py).
```

**Held fixed across every arm** (so the only free variable is context quality):
generation model, system prompt, user prompt (test diff + failing output +
seed-function sources), context token budget, and the seed symbols. The
provider context block is placed **last** in the prompt so the per-task prefix
is byte-identical across arms (prompt-cacheable on backends that support it).

**Judge = the repo's own tests.** No LLM-as-judge, no rubric. A patch either
makes the mined tests pass or it does not. The applier tries a cleanest-first
cascade (`git apply -p1` → `--recount` → `--3way` → `-p0` → `patch --fuzz`) so
a well-formed diff applies exactly while a slightly-off diff still gets a fair
chance; the winning strategy is recorded per row.

---

## 3. What the harness already guarantees (implemented)

These are done and validated — do not rebuild them:

1. **Dependency-free REST backends.** `gemini`, `groq`, and `openrouter` go
   over plain HTTP via `requests` (no vendor SDK). `anthropic` remains the
   SDK/paid arm. Keys are read from the environment only.
2. **Gold-validity runtime gate.** Before scoring any provider on a task, a
   real run re-verifies gold→PASS *in the current environment* and skips
   un-judgeable tasks to `<repo>.skipped.jsonl` (defends against pytest/dep
   drift). Disable with `--skip-gold-gate`.
3. **Transient-error safety.** 429/5xx/network failures retry within a call
   (honoring server `retryDelay`); a hard per-day quota is not retried. Rows
   left by a transient failure are **not** counted as "done" on resume and are
   **dropped** from `--report` — a rate cap or server blip can never be
   recorded as a failed fix.
4. **Resumability + dedupe.** Runs append to `<repo>.jsonl` and resume by
   `(commit, provider, sample)`. `--report` keeps the last real measurement per
   key, so a retried task never double-counts.
5. **Judge self-tests.** `--mock gold` (every task must PASS) and `--mock
   empty` (every task must FAIL) write to separate `.mock.gold.jsonl` /
   `.mock.empty.jsonl` files.
6. **Free-tier throttle (`--sleep SECONDS`).** Pauses between generations to
   stay under a per-minute request cap; default 0 preserves prior behavior.
7. **Pooled cross-repo report.** `--report` accepts multiple JSONL paths and
   prints each per-repo section plus a POOLED section over all of them (the
   fix for the per-repo power problem — commits are distinct SHAs so pooling
   just widens the paired sample).
8. **Effect size in the report.** Each pairwise line reports the paired
   pass-rate delta and matched-pairs rank-biserial alongside p / Holm-p, so a
   null reads "no effect, delta≈0," not just "p>0.05."

---

## 4. Implementation gaps — the work still to do

Items T1–T3 below are **now implemented** (see §3.6–3.8); they are kept here
for traceability. T4–T6 remain. Each has a definition of done.

### T1 — free-tier throttle (`--sleep SECONDS`) · ✅ DONE (§3.6)
### T2 — pooled cross-repo report · ✅ DONE (§3.7) — pass multiple files to `--report`
### T3 — effect size in the report · ✅ DONE (§3.8) — delta + rank-biserial

### T4 — post-cutoff task mining  · the contamination escape (see §9)
The benchmark repos are public and their gold fixes are in the model's
training data. **Done when:** `tasks.py` can restrict mining to commits
authored after a caller-supplied date (`--since YYYY-MM-DD`, set to the
generation model's training cutoff) and a post-cutoff task set exists for
≥2 repos. If a `--since` filter is absent it must be added.

### T5 — httpx (and more) task coverage  · breadth for pooling
`httpx.json` has 0 tasks; requests has only 4. **Done when:** each repo used in
the pooled analysis has ≥10 validated tasks, or is explicitly dropped from the
pool with a recorded reason.

### T6 — second generation model  · required by acceptance A2
A single model is a single point of confounding. **Done when:** the full sweep
is replicated on a second model (`--backend groq`, an independent vendor) and
the ordering of arms is compared across models.

---

## 5. Prerequisites

- **Keys (env only, never committed, never on the command line):**
  `GEMINI_API_KEY` (free — Google AI Studio) and, for T6, `GROQ_API_KEY`
  (free — Groq console).
- **Software:** Python 3.9+, `git`, `patch`, `pytest`, and `requests` (already
  present). No vendor SDK for the free-tier backends.
- **Repos:** `benchmark_repos/<name>` clones (present for click, flask,
  requests, rich, starlette, httpx, …).
- **Compute:** none beyond CPU. $0 on free tiers (rate-limited, not metered).

---

## 6. Execution order

Each phase gates the next. Do not spend generation budget before the judge is
validated (Phase 1).

### Phase 0 — environment check (no key)
```bash
python benchmarks/downstream/run_eval.py --help          # backends list groq/gemini
python -c "import requests; print('requests OK')"
```

### Phase 1 — validate the judge, per repo (no generation budget)
```bash
# gold must be ALL pass; empty must be ALL fail. If any gold task fails, it is
# un-judgeable in this env and the gold gate will skip it in real runs.
python benchmarks/downstream/run_eval.py \
  --tasks benchmarks/downstream/tasks/click.json \
  --repo benchmark_repos/click --mock gold
python benchmarks/downstream/run_eval.py \
  --tasks benchmarks/downstream/tasks/click.json \
  --repo benchmark_repos/click --mock empty --providers none
```
**Gate A1:** gold self-test = 100% PASS on every task that is not gold-gate
skipped; empty self-test = 0% PASS.

### Phase 2 — the real sweep, model #1 (Gemini, free)
```bash
export GEMINI_API_KEY=...
for repo in click flask requests rich starlette; do
  python benchmarks/downstream/run_eval.py \
    --tasks benchmarks/downstream/tasks/$repo.json \
    --repo benchmark_repos/$repo \
    --backend gemini --model gemini-flash-latest \
    --samples 3 --sleep 4
done
```
Resumable — safe to re-run after a rate cap; transient rows retry
automatically. Per-task mean over `--samples 3` averages per-generation noise.

### Phase 3 — analysis
```bash
# per repo
python benchmarks/downstream/run_eval.py --report benchmarks/downstream/results/click.jsonl
# per-repo sections + POOLED (just pass every file; pooling is automatic)
python benchmarks/downstream/run_eval.py --report \
  benchmarks/downstream/results/{click,flask,requests,rich,starlette}.jsonl
```

### Phase 4 — replicate on model #2 (Groq, free) — acceptance A2
Repeat Phase 2 with `--backend groq`; compare arm ordering across models.

### Phase 5 — contamination control (T4)
Mine post-cutoff tasks, rerun Phases 2–3 on them, and compare the `none`-arm
pass rate against the public-repo run (a large drop confirms memorization was
inflating the floor).

---

## 7. Analysis plan

- **Primary statistic:** paired Wilcoxon signed-rank over per-task mean pass
  rate, arms compared to the highest-scoring arm, **Holm-corrected** within the
  family (`benchmarks/significance.py`, already used by `--report`).
- **Report per-repo AND pooled.** Per-repo results are underpowered; the pooled
  test is the headline, per-repo is the robustness check.
- **Effect size + direction always** (T3). A p>0.05 is reported as "no
  detectable effect, delta = x ± CI," never as silent omission.
- **The `none` arm is the contamination probe.** Its pass rate is the
  memorization + seed-source floor. Only the *paired deltas between arms* carry
  the context-quality signal, because memorization applies equally to every arm
  (same model, same task). If `none` saturates, the task set cannot
  discriminate → go to Phase 5.

---

## 8. Results — PENDING (fill only from `--report`)

> These tables are intentionally empty. A cell is written only when it traces
> to a committed `.jsonl` row. See §11.

**Table 1 — per-provider pass rate (model: `gemini-flash-latest`, samples=3)**

| repo | n tasks | diffcontext | diffcontext_gap | bm25 | samefile | none |
|---|---|---|---|---|---|---|
| click |  |  |  |  |  |  |
| flask |  |  |  |  |  |  |
| requests |  |  |  |  |  |  |
| rich |  |  |  |  |  |  |
| starlette |  |  |  |  |  |  |
| **pooled** |  |  |  |  |  |  |

**Table 2 — paired significance vs. top arm (Wilcoxon, Holm-corrected)**

| comparison | delta | p | Holm p | effect size | n_eff |
|---|---|---|---|---|---|
| top vs bm25 |  |  |  |  |  |
| top vs samefile |  |  |  |  |  |
| top vs none |  |  |  |  |  |

**Table 3 — cross-model replication (model #2: Groq)**

| repo | diffcontext | bm25 | none | ordering matches model #1? |
|---|---|---|---|---|
|  |  |  |  |  |

---

## 9. Threats to validity (state these; don't let a reviewer find them)

- **Oracle localization.** Arms are seeded with the gold-changed symbols, so
  the measured claim is "given correct localization, does context quality
  matter?" — not end-to-end issue solving. This isolates the variable
  DiffContext controls and makes the numbers **non-comparable** to SWE-bench.
- **Contamination.** Public repos, gold fixes in training data. Mitigations:
  the `none` probe (§7) and the Phase 5 post-cutoff run (T4). If `none`
  saturates on public repos, no conclusion is drawn until the post-cutoff run.
- **Task family bias.** Test-verified fix tasks skew toward well-tested,
  localized changes; cross-subsystem changes rarely produce a clean
  fail@state/pass@gold pair. This under-samples exactly the bucket where
  retrieval should matter most — the bias runs **against** finding an effect,
  which is the acceptable direction.
- **Power.** ~11–15 tasks/repo; only large effects clear p<0.05 per repo.
  Pooling (T2) is mandatory before any null claim.
- **Single-model confounding.** One model's habits could drive the result;
  T6/Phase 4 is the guard.
- **Judge brittleness.** Strict-warning repo configs can make a test file fail
  to collect under a newer pytest; the gold gate (§3.2) removes such tasks
  rather than scoring every arm as fail on them.

---

## 10. Acceptance criteria (the bar for citing any number)

| # | Gate |
|---|---|
| A1 | Judge validated: `--mock gold` 100% PASS, `--mock empty` 0% PASS, per repo |
| A2 | Result replicated on ≥2 independent generation models with consistent arm ordering |
| A3 | ≥20 tasks in the pooled analysis, from ≥2 repos |
| A4 | Every comparison reports p, Holm-corrected p, **and** effect size + direction |
| A5 | `none`-arm floor reported; if it saturates, conclusions defer to the post-cutoff run |
| A6 | Every threat in §9 disclosed in the write-up |
| A7 | Every results cell reproducible from a recorded `.jsonl` line |

---

## 11. Integrity

- **No fabricated numbers.** Results tables ship blank and are filled only from
  `--report` over recorded rows. No cell is ever hand-entered.
- **The null is a result.** If `diffcontext` does not beat `bm25`, that is the
  finding and it is published as such. The plan forbids selectively re-running
  to manufacture a win.
- **Keys never touch the repo.** Read from the environment only; rotate any key
  that has been exposed.
