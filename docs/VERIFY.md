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

One class of insufficiency is **structurally invisible to every predictor
above, by construction**: cross-subsystem conceptual coupling — code related
to your change through shared *meaning* rather than any call, import, name,
or file relationship. The canonical example from the benchmark's failure
taxonomy: a settings flag and the security check that reads it through a
config lookup. On hand-audited pairs of exactly this kind, graph, BM25, and
the hybrid blend all scored **0/20 recall** (see
`benchmarks/EVAL_V2_REPORT.md`). No static signal reaches these — a perfect
sufficiency score and "graph confidence: 100%" are both fully consistent
with such a partner existing and being absent from context. This is why the
compiled meta-header carries a permanent note that graph confidence means
*structural* completeness only, and why git co-change history (which does
see this class) is the roadmap's fourth signal.

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

**The score is evidence-aware.** A component with zero observations behind
it (no direct neighbors, no outgoing edges, nothing the ranker scored as
relevant) says nothing, so it must not count as a perfect 1.0 — that
defect made the old formula report a constant ~100 on sparse graphs
(σ=9.9 on Python at n=1080, σ=5.0 on TypeScript; correlation with
measured recall: none). The score now shrinks toward 50 ("don't know")
in proportion to missing evidence and prints the evidence fraction.
After the fix, pooled correlation with recall is r≈0.29 (p=0.0001) on
both Python (n=1080, 9 repos) and TypeScript (n=360, 3 repos) — a real
ranking signal, though still not a probability, which is what
calibration below is for.

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

**Measured at scale** (1,080 mined cases across 9 Python repos,
`benchmarks/calibration_at_scale.py`): score/100 read directly as a
probability of recall loses to just predicting the mean (MAE 0.48 vs
0.34) — the raw score is a *ranking* signal, never a probability. What
does predict recall out-of-repo is a least-squares fit over the runtime
features `verify` already computes (score components + selected /
missing-direct / dropped counts + tokens): it beat the predict-the-mean
baseline on held-out MAE in 8/9 repos (held-out r up to 0.65). That fit
is now a product feature:

```bash
diffcontext verify --from-history 60 --calibrate --save-calibration
# ...fits and writes .diffcontext-calibration.json, then later:
diffcontext verify --ref HEAD~1
# ...ends with: Calibrated recall estimate: 74% (fit on 60 of this
#    repo's own history cases — see .diffcontext-calibration.json)
```

The fit refuses to run under 30 cases (a model fit on a handful of
points is noise with a JSON file), and re-weighting the four score
components *alone* was measured to have no held-out predictive power —
the count features carry the signal. Both facts are in the benchmark
report, nulls included.

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
4. **Done — learned calibration** (`--save-calibration`): a per-repo
   recall predictor fit on the calibration data `verify` collects.
   Measured before shipping: naively re-fitting the four component
   weights has ~zero held-out power (reported as the null it is); the
   extended runtime-feature fit generalizes in 8/9 held-out repos.
5. **Q2 harness:** `verify --llm` mode — for each case with a `task`, ask a
   model for a patch given the compiled context, apply it, run the repo's
   tests, and report end-task pass rate alongside recall. This is the first
   rung that measures the thing users actually care about.
6. **Cross-language:** the case format is language-agnostic already; the
   parser/graph is Python-only. Each new language re-runs the same ladder.
