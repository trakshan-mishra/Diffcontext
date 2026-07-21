# Downstream Evaluation — Execution Order & Technical Plan

**Program:** DiffContext rung-5 downstream evaluation ("does better context
improve LLM task outcomes?")
**Purpose of this document:** a single, self-contained order that a product
manager and a technical lead can hand to an operator to run the evaluation
end-to-end on a **zero-budget (free-tier only)** footprint, and turn the
output into a research-grade result.
**Non-negotiable:** no fabricated numbers. Every results table in this plan
ships **empty** and is filled **only** from real runs recorded in
`benchmarks/downstream/results/*.jsonl`. If a run did not happen, its cell
stays blank.

---

## 1. Objective and research question

Everything measured in DiffContext so far is **proxy retrieval quality**
against co-change ground truth (real, already in-repo — see §3). The
question a reviewer and a user actually ask is unanswered:

> **RQ.** With the model, prompt, token budget, and code localization held
> fixed, does changing **only** the context provider change whether an
> LLM's generated patch makes the repository's own test suite pass?

This is a **falsifiable, controlled** question. The null hypothesis is
"context provider has no effect on pass rate." We reject or fail to reject
it per provider pair with a paired significance test.

---

## 2. Acceptance criteria (what makes the result paper-worthy)

The program is **done** when all of the following are true. These are the
gates; partial completion is explicitly allowed and disclosed, never hidden.

| # | Criterion | Threshold |
|---|-----------|-----------|
| A1 | Judge validated | `--mock gold` = 100% PASS, `--mock empty` = 100% fail, on every repo used |
| A2 | ≥ 2 independent models | full provider sweep completed on ≥ 2 models (e.g. Gemini + Groq-hosted) |
| A3 | Task count | ≥ 20 validated tasks total across repos (more = stronger; state the number) |
| A4 | Statistical test reported | paired Wilcoxon + Holm correction, per §11, with effect size |
| A5 | Ordering holds | `diffcontext ≥ bm25 ≥ none` on point estimates on **both** models |
| A6 | Threats disclosed | oracle-localization, contamination, single-ecosystem limits stated in prose |
| A7 | Reproducible | one command sequence (this plan) reproduces the JSONL from pinned SHAs |

> **Honest-null clause.** If A5 fails — e.g. `diffcontext` does not beat
> `bm25` — that is a **publishable finding**, not a failure to suppress.
> The paper reports it. Do not re-run selectively to manufacture a win.

---

## 3. Background — what is already real (cite, do not re-measure)

These numbers are **already measured** and live in the repo; they are the
*prior* the downstream eval extends. Sources:
`docs/BENCHMARKS.md`, `benchmarks/RIGOR_REPORT_2026-07.md`.

| Signal (retrieval quality) | Hit | Recall |
|---|---|---|
| Call graph alone | 0.748 | 0.558 |
| BM25 keywords alone | 0.822 | 0.619 |
| Same file alone | 0.693 | 0.506 |
| **Hybrid (product)** | **0.868** | **0.705** |

- Leave-one-repo-out validated blend `[0.3, 0.5, 0.2]`; held-out repos never
  used for tuning: black 0.897/0.712, requests 0.953/0.762, rich 0.844/0.760,
  starlette 0.929/0.776.
- **Disclosed weakness:** cross-repo mean **precision < 0.1** at default
  top-k; `--cutoff gap` lever gives ~4× precision at ~30% recall cost.
- Calibration at n=1080; sufficiency signal calibrated across two languages.

**What is missing and this program supplies:** the downstream (task-outcome)
result. Nothing in `benchmarks/downstream/results/` has been produced yet.

---

## 4. Scope

**In scope:** the controlled downstream eval on mined Python bug-fix tasks;
free-tier LLM generation; tests-as-judge grading; paired reporting.

**Out of scope (this program):** localization/agentic retrieval (tasks are
oracle-localized — a stated limitation), non-Python ecosystems, fine-tuning,
and published-system head-to-heads (Aider repo-map etc. — a *follow-on*
program, see §14).

---

## 5. Roles (RACI, minimal)

| Activity | Product Mgr | Tech Lead | Operator |
|---|---|---|---|
| Approve scope & acceptance criteria | **A/R** | C | I |
| Provision free API keys | I | C | **R** |
| Environment setup & judge validation | I | **A** | **R** |
| Execute sweeps, monitor quota/429 | I | C | **R** |
| Report + significance | C | **A/R** | R |
| Draft paper sections / threats | **A** | R | C |

---

## 6. Prerequisites

**Accounts / keys (all free, no card):**
- Google AI Studio key (`GEMINI_API_KEY`) — https://aistudio.google.com/apikey
- Groq key (`OPENAI_API_KEY` via Groq) — https://console.groq.com/keys

**Software:**
- Python 3.9+ (repo floor); git with **full history** for benchmark repos
- `pip install rank_bm25 "numpy<2.3" pytest google-genai openai`
- Each benchmark repo's own runtime deps (`pip install -e benchmark_repos/<repo>`)

**Hardware:** none beyond a laptop. Generation is remote (free APIs);
grading is local `pytest` (CPU-light). **No GPU, no local model.**

**Data (already in repo):** pre-mined, machine-validated task files
`benchmarks/downstream/tasks/{click,flask,httpx,requests,rich,starlette}.json`.

---

## 7. Requirements

**Functional**
- F1 Same model, prompt, budget, seeds across all arms; only the provider
  context block varies (guaranteed by `run_eval.py` prompt assembly).
- F2 Judge = repo's own tests; PASS ⟺ `pytest` exit 0 on the patched worktree.
- F3 Resumable: re-running the same command skips completed `(commit,
  provider, sample)` rows.
- F4 Per-model isolation: `--tag <model>` writes `<repo>.<tag>.jsonl` so
  models never share a file (resume key has no model field).

**Non-functional**
- N1 Zero monetary cost — free tiers only.
- N2 Rate-limit safe — `--sleep` throttle + built-in 429 backoff; daily caps
  handled by resume across days.
- N3 Reproducible — pinned upstream SHAs in task files; deterministic judge.
- N4 Integrity — results are append-only JSONL; no manual edits to result files.

---

## 8. Resource plan — APIs

| Role | Backend flag | Model (example) | Endpoint | Cost | Notes |
|---|---|---|---|---|---|
| Model 1 | `--backend gemini` | `gemini-2.5-flash` | Google AI Studio | $0 | Flash, not Pro — Pro free tier too tight |
| Model 2 | `--backend openrouter` + `OPENAI_BASE_URL` | Groq coding model (e.g. Kimi K2 / Qwen-Coder) | `https://api.groq.com/openai/v1` | $0 | Verify model id against `/v1/models` |

> Rate limits on free tiers **change**; confirm current RPM/RPD at each
> provider's docs before sizing a run. Do not hard-code assumed limits into
> the paper — cite the run's recorded `usage` instead.

---

## 9. Method (design that makes it controlled)

```
tasks.py    mine commits changing code AND tests; keep only tasks that
            machine-verifiably FAIL at parent+tests and PASS at the fix
run_eval.py per (task × provider): compile context at task state → ask model
            for a unified diff → apply (tolerant applier cascade) → run tests
--report    per-provider pass rate + paired Wilcoxon (Holm) over tasks
            common to all providers
```
- **Held fixed:** model, system+user prompt, token budget, oracle seeds.
- **Varied:** the provider context block only (placed last; prompt-cacheable).
- **Providers:** `diffcontext`, `diffcontext_gap`, `bm25`, `samefile`, `none`.
- **`none` = memorization probe:** if it passes often, absolute rates are
  inflated by pretraining; paired deltas remain interpretable.

---

## 10. Execution order (phased)

### Phase 0 — Setup (Operator; ~15 min)
```bash
git clone https://github.com/pallets/click.git benchmark_repos/click
pip install rank_bm25 "numpy<2.3" pytest google-genai openai
pip install -e benchmark_repos/click
```

### Phase 1 — Judge validation (GATE A1; free, no key)
```bash
python -m benchmarks.downstream.run_eval --tasks benchmarks/downstream/tasks/click.json \
    --repo benchmark_repos/click --mock gold  --tag selftest
python -m benchmarks.downstream.run_eval --tasks benchmarks/downstream/tasks/click.json \
    --repo benchmark_repos/click --mock empty --providers none --tag selftest
```
**Pass condition:** gold = all PASS, empty = all fail. If not, fix env
(usually a missing repo dep) before spending any quota.

### Phase 2 — Model 1 sweep (Gemini Flash)
```bash
export GEMINI_API_KEY=...
python -m benchmarks.downstream.run_eval \
    --tasks benchmarks/downstream/tasks/click.json --repo benchmark_repos/click \
    --backend gemini --model gemini-2.5-flash --sleep 8 --tag gemini-flash
```

### Phase 3 — Model 2 sweep (Groq)
```bash
export OPENAI_BASE_URL=https://api.groq.com/openai/v1
export OPENAI_API_KEY=...              # Groq key
python -m benchmarks.downstream.run_eval \
    --tasks benchmarks/downstream/tasks/click.json --repo benchmark_repos/click \
    --backend openrouter --model "moonshotai/kimi-k2-instruct" --sleep 3 --tag groq-kimi
```

### Phase 4 — Scale up (repeat P2/P3 per repo)
```bash
git clone https://github.com/pallets/flask.git   benchmark_repos/flask
git clone https://github.com/encode/httpx.git    benchmark_repos/httpx
git clone https://github.com/psf/requests.git    benchmark_repos/requests
# pip install -e each; then re-run P2/P3 with that repo's tasks + --repo
```

### Phase 5 — Report (GATE A4/A5)
```bash
python -m benchmarks.downstream.run_eval --report benchmarks/downstream/results/click.gemini-flash.jsonl
python -m benchmarks.downstream.run_eval --report benchmarks/downstream/results/click.groq-kimi.jsonl
```

---

## 11. Results — to be filled from real runs ONLY

> Populate strictly from `--report` output. Blank = not yet run. Never
> estimate, interpolate, or copy a number the harness did not print.

**Table 1 — Per-provider pass rate (paired over common tasks).**

| Provider | Gemini-Flash | Groq-Kimi |
|---|---|---|
| diffcontext | | |
| diffcontext_gap | | |
| bm25 | | |
| samefile | | |
| none | | |
| _n common tasks_ | | |

**Table 2 — Paired Wilcoxon vs. top provider (Holm-corrected).**

| Model | pair | p | p_holm | n_eff | effect size |
|---|---|---|---|---|---|
| | | | | | |

Effect size (add per A4): compute Cliff's δ or matched-pair rank-biserial
from the paired vectors; the harness prints p and n_eff, δ is computed
alongside.

---

## 12. Statistical & rigor requirements (for the paper)

- **Power / n:** Wilcoxon needs ≥ 6 complete tasks to run; the paper wants
  more. State the exact n; do not claim significance below the harness's
  minimum. If underpowered, report descriptively and say so.
- **Multiple comparisons:** Holm within each family (already in `--report`).
- **Effect size beside every p** (A4).
- **Two models minimum** (A2) so the claim is model-general, not a quirk.
- **Samples:** `--samples k` (k>1) captures generation nondeterminism; report
  mean pass over samples per (provider, task). Budget permitting.

---

## 13. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Per-minute 429 | High (free tier) | Low | `--sleep`; built-in backoff honors `retryDelay` |
| Per-day quota exhausted | High | Low | Resume next day (same command); run one provider at a time |
| Small task count → underpowered | Med | High | Scale via Phase 4; report n honestly; descriptive if <6 |
| Pretraining contamination (`none` passes) | Med | Med | Report `none` rate explicitly; rely on paired deltas; note as threat |
| Oracle localization inflates realism | Certain | Med | Disclose in prose; frame claim as "given correct localization" |
| Model refuses / emits no diff | Low | Low | Recorded as `gen_error`; counts as fail; report rate |
| Weak free model → floor effect | Med | Med | If pass≈0 everywhere, signal is lost — switch to a stronger free model |

---

## 14. Deliverables

1. **Raw data:** `benchmarks/downstream/results/*.jsonl` (committed).
2. **Filled Tables 1–2** in the paper, sourced only from `--report`.
3. **Threats-to-validity** paragraph (oracle localization, contamination,
   single ecosystem, free-model ceiling).
4. **Reproduction appendix:** this plan + pinned SHAs.
5. **(Follow-on program, not this order):** published-system baselines —
   Aider repo-map head-to-head at equal budget; a code-tuned dense retriever;
   positioning vs RepoGraph / CodexGraph / Agentless (see `docs/ROADMAP.md` §4).

---

## 15. Milestones

| M | Exit criterion |
|---|---|
| M0 Setup + judge validated | A1 green on click |
| M1 First real signal | click swept on both models; Tables 1–2 have click rows |
| M2 Scale | ≥ 20 tasks across ≥ 3 repos on both models (A2, A3) |
| M3 Report | significance + effect sizes computed (A4, A5) |
| M4 Paper-ready | threats + reproduction written (A6, A7) |

---

## 16. Integrity statement

No result in any deliverable derived from this plan may be entered by hand,
estimated, or carried over from a different model/run. Every cell traces to a
line in a committed `*.jsonl` and a printed `--report`. A failed hypothesis is
reported as found. This is the condition under which the work is
research-paper-worthy.
