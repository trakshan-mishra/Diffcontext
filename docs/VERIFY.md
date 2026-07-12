# `diffcontext verify` — sufficiency scoring, test cases, and calibration

`compile` answers *"here is relevant context."*
`verify` answers the harder question: **"is this context sufficient — and how would you know?"**

It has three modes, each one step further up the evidence ladder:

| Mode | Command | What it proves |
|---|---|---|
| Sufficiency report | `diffcontext verify --ref HEAD~1` | Structural completeness of one compiled context |
| Test cases | `diffcontext verify --cases cases.json` | Your own known-true expectations, measured |
| Calibration | `diffcontext verify --from-history 30 --calibrate` | Whether the score itself can be trusted on *your* repo |

---

## The honesty contract (read first)

The sufficiency score is a **structural proxy, not a probability**. True
sufficiency is defined relative to a stochastic model (does the LLM produce
a correct patch?) and cannot be proven statically — anyone claiming a hard
guarantee here is overclaiming. What *can* be measured statically is the
set of known structural predictors of insufficiency:

1. **Direct-neighbor closure** — a caller/callee of a changed symbol that is
   *not* in context is the strongest predictor of a wrong or hallucinated patch.
2. **High-score retention** — symbols the ranker itself scored as relevant
   but the token budget cut. The ranker is telling you its own output is incomplete.
3. **Local graph confidence** — unresolved calls out of the changed symbols
   (externals, dynamic dispatch) mean the graph may be blind to real dependencies.
4. **Parse health** — files with SyntaxErrors are invisible to the graph.

The score becomes **calibrated confidence** only after `--calibrate` maps
score buckets to empirically measured recall on your repo. Until then, treat
it as a ranked warning system. If calibration comes back flat or negative,
`verify` says so in plain text ("NULL RESULT") instead of hiding it — a
proxy that doesn't track reality on your repo should not be trusted there,
and knowing that is worth more than a decorative number.

---

## Mode 1: sufficiency report

```bash
# For the last commit's changes:
diffcontext verify --ref HEAD~1

# For a hypothetical change:
diffcontext verify --changed ./auth.py:validate_jwt --max-tokens 8000

# Machine-readable, for CI or a harness:
diffcontext verify --ref HEAD~1 --json
```

Output:

```
=== DIFFCONTEXT SUFFICIENCY REPORT ===
Verdict : ⚠ DEGRADED  (structural score: 71/100)
  direct-neighbor closure : 83%  (2 missing)
  high-score retention    : 59%  (9 relevant symbols cut by budget)
  local graph confidence  : 100%
  parse health            : 100%

FINDINGS:
  ✗ [missing-direct-neighbor] 2 direct caller(s)/callee(s) of the changed
    symbols are NOT in context. ... Remediation: raise --max-tokens or --top-k.
      - ./api.py:get_user
      - ./middleware.py:check_auth
```

The exit code is `0` only for `SUFFICIENT`, so CI can gate on it:

```yaml
# .github/workflows/context-gate.yml (sketch)
- run: pip install diffcontext && diffcontext verify --ref origin/main --repo .
```

Verdicts: `SUFFICIENT` (score ≥ 80), `DEGRADED` (≥ 55), `INSUFFICIENT` (< 55).
Weights: 45% direct closure, 30% high-score retention, 15% local confidence,
10% parse health — direct closure dominates because a missing direct neighbor
is the failure mode the eval_v2 benchmark observed most often.

---

## Mode 2: your own test cases

A test case states something **you know to be true about your repo**:
"when `validate_jwt` changes, a correct context must include `get_user`."
You know these because you wrote the code, fixed the incidents, reviewed
the PRs. The tool is then graded against *your* knowledge, not its own.

### Case file format

JSON (always works) or YAML (if PyYAML is installed):

```json
{
  "version": 1,
  "defaults": { "budget": 10000, "depth": 2, "top_k": 20, "min_recall": 1.0 },
  "cases": [
    {
      "name": "jwt-validation-change",
      "task": "tighten JWT expiry validation without breaking session refresh",
      "changed": ["./auth.py:validate_jwt"],
      "must_include": ["./api.py:get_user", "./middleware.py:check_auth"],
      "must_exclude": ["./billing.py:invoice_total"],
      "budget": 8000,
      "min_recall": 1.0
    },
    {
      "name": "order-total-refactor",
      "changed": ["./orders/pricing.py:compute_total"],
      "must_include": ["./orders/checkout.py:finalize", "./orders/tax.py:tax_for"]
    }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `changed` | ✓ | Symbol IDs treated as the modified code (`./file.py:func` or `./file.py:Class.method`) |
| `must_include` | ✓ | Symbols a sufficient context MUST contain (recall target) |
| `must_exclude` | | Symbols that must NOT appear (precision guard — catches over-retrieval) |
| `task` | | Plain-English intent. Recorded in results; reserved for future query-aware ranking |
| `budget` | | Token budget (`0` = unlimited). Default 10000 |
| `top_k` | | Max context symbols per changed symbol (`0` = unlimited). Default 20 |
| `depth` | | Dependency traversal depth. Default 2 |
| `min_recall` | | Pass threshold on `must_include` recall. Default 1.0 |

### How a case is checked

1. The repo is indexed once; each case runs the **real pipeline**
   (impact analysis → hybrid scoring → budget selection) with its own budget.
2. `recall = |must_include ∩ selected| / |must_include|`.
3. **Pass** = `recall ≥ min_recall` **and** no `must_exclude` symbol selected.
4. A symbol that doesn't exist in the index **counts as a miss** — never a
   silent skip — and is flagged with a fuzzy-match suggestion
   (`'create_ordr' not found — did you mean './service.py:create_order'?`),
   so a typo can't quietly inflate or deflate your numbers.

```bash
diffcontext verify --cases cases.json           # human-readable
diffcontext verify --cases cases.json --json    # for scripts
```

Exit code is `0` only if every case passes.

### What makes a good case

- **Write cases from incidents.** "The bug in PR #212 happened because the
  agent didn't see `refresh_session`" → that's a case, verbatim.
- **One behavior per case.** Three focused cases beat one case with nine
  `must_include` entries — failures stay diagnosable.
- **Add `must_exclude` for your known false-positive magnets** (that giant
  utils module the ranker loves) so precision regressions get caught too.
- **Cases you didn't hand-pick are worth more** — which is what
  `--from-history` provides.

---

## Mode 3: calibration — checking the checker

```bash
# Mine up to 30 real cases from your git history and grade against them:
diffcontext verify --from-history 30 --calibrate

# Or generate them to a file first, prune the noise, then run:
diffcontext verify --from-history 50 --out cases.json
diffcontext verify --cases cases.json --calibrate
```

History cases come from **co-change ground truth**: if a past commit
modified `alpha()` and `beta()` together, that's external evidence (human
behavior, not our graph) that they're related — so given `alpha` as the
query, a good context should retrieve `beta`. This is the same methodology
as the eval_v2 benchmark, shipped inside the tool. History cases default to
`min_recall: 0.5` because commits are noisy — they touch unrelated code too.

`--calibrate` then answers the meta-question: **does the structural
sufficiency score track measured recall on this repo?**

```
=== CALIBRATION: structural score vs measured recall ===
Cases: 25
  score  40-60 : n=1   mean recall  28.6%  #####
  score  80-100: n=24  mean recall  57.7%  ###########
Pearson r (score vs recall): +0.274
→ Weak positive relationship. Treat the score as a coarse warning signal...
```

That output above is real — it's this repo's own history, and +0.274 is a
*weak* correlation, reported as such. This is the point of the design:
the tool measures itself against evidence it didn't choose and reports the
result even when unflattering.

---

## How to measure "model accuracy" honestly

There are two different questions, and conflating them is the most common
way context-tool claims go wrong:

**Q1 — Retrieval accuracy (what `verify` measures):** did the context
contain the code a correct answer needs? Measured as recall against
ground truth you didn't invent (your cases, git co-change). Cheap,
deterministic, runs in CI.

**Q2 — End-task accuracy (what `verify` does NOT measure):** given this
context, did the *LLM* produce a correct patch/answer? This requires an
LLM in the loop: run the same task with context variant A vs B, apply the
patches, run the test suite, compare pass rates. Expensive, stochastic,
model-version-dependent.

Q1 is a *proxy* for Q2 — a necessary-but-not-sufficient condition: missing
context nearly guarantees a wrong answer; complete context doesn't
guarantee a right one. The honest pipeline is therefore:

1. Maximize and *measure* Q1 (this tool, today).
2. Calibrate the structural score against Q1 (`--calibrate`, today).
3. Periodically spot-check Q2 on a small fixed task set with a real model,
   using the `task` field as the prompt and repo tests as the judge
   (roadmap — see below). Never let a Q1 number be quoted as a Q2 claim.

---

## Advancement roadmap (each rung measurable before the next)

1. **Done — sufficiency + cases + calibration** (this document).
2. **Corpus growth:** ship `cases.json` files for the 6 benchmark repos;
   accept community case contributions — cases written by people who didn't
   build the tool are the highest-value eval data.
3. **Task-aware ranking:** blend the case's `task` text into the BM25 signal
   (the query text is already recorded per case, so the A/B is one flag:
   recall with vs without). Only merge if recall improves on cases.
4. **Learned calibration:** replace the fixed component weights
   (45/30/15/10) with weights fit on accumulated case results per-repo —
   the calibration data `verify` already collects *is* the training set.
5. **Q2 harness:** `verify --llm` mode — for each case with a `task`, ask a
   model for a patch given the compiled context, apply it, run the repo's
   tests, and report end-task pass rate alongside recall. This is the first
   rung that measures the thing users actually care about.
6. **Cross-language:** the case format is language-agnostic already; the
   parser/graph is Python-only. Each new language re-runs the same ladder.
