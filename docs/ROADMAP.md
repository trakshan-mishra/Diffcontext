# Roadmap (post-rigor-pass, 2026-07-20)

Every item states its measured motivation. Items land in this order
unless a measurement says otherwise; each rung must be validated on
repos not used for its tuning before its claim is made (house rule).

## 1. LLM-judged downstream evaluation (rung 5) — the one missing metric family

Everything measured so far is proxy retrieval quality against co-change
ground truth. The question a paper reviewer (and a paying user) actually
asks — *does better context improve LLM task outcomes?* — is unanswered.
Design, already agreed: fixed model + fixed token budget, swap only the
context provider (hybrid / BM25 / dense / grep-packing / gap-cutoff),
generate patches for real tasks, judge with the repo's own test suite
(SWE-bench-Lite-style subset plus `verify` cases with `task` fields).
Needs LLM API access + budget. This eval is three things at once:
the headline result, the de-contamination prerequisite for using
co-change as a *ranking* signal (it is currently the eval's ground
truth), and the external validation of the co-change proxy itself.

## 2. Ship the measured wins from the rigor pass

- ~~**Blend weights → [0.3, 0.5, 0.2].**~~ **Shipped 2026-07-20.**
  `HYBRID_WEIGHTS` updated, floors re-frozen, CHANGELOG noted; flask
  gate re-run confirms the recorded LORO numbers (hit 0.863 / recall
  0.694) through the product path.
- ~~**`--cutoff gap` option.**~~ **Shipped 2026-07-20** on `compile` and
  `verify` (opt-in; top-k stays the recall-first default), together with
  a `precision_lb` column in verify results so the tradeoff is
  measurable per-repo.
- **Dense leg as `[dense]` extra.** The only statistically significant
  recall gains measured (flask/httpx/pydantic, p<0.05) and the only
  signal cracking the cross-subsystem bucket — but it drags in
  sentence-transformers/torch, so opt-in extra, never default.
  Weights [0.2, 0.35, 0.2, 0.25] from the LORO run.

## 3. Graph coverage fixes (both have named, measured failure modes)

- **Override edges** across class hierarchies — the backend_dispatch
  bucket is 0/20 for the graph today.
- **if/try/with collector gap** — `def`s under `if TYPE_CHECKING:` /
  `try-except ImportError` get zero edges
  (`graph_builder._collect_function_nodes`, documented in its docstring).
- Re-run eval_v2 + buckets after each; the regression gate catches losses.

## 4. Positioning against published systems

The benchmark compares signal families, not named competitors. Minimum
for a paper: a head-to-head against Aider's repo-map at equal token
budgets on the same co-change queries, plus a conceptual-comparison
table (RepoGraph / CodexGraph / Agentless-style localization). Also run
one code-tuned embedding model — all-MiniLM is an NL encoder, so the
current dense numbers are a floor, not a verdict.

## 5. Co-change as a ranking signal, then learned ranking

Blocked behind item 1 (contamination: co-change is the current eval's
ground truth; needs the independent downstream eval or a strict temporal
split first). The cross-subsystem bucket is the prize — history is the
only signal family with a path to the 15/20 that nothing content-based
reaches.

## 6. TypeScript to parity

CommonJS support (`exports.x =` — the measured 0%), then the five-repo
benchmark methodology applied to TS repos (mined-case smoke numbers are
not benchmark numbers), then per-language blend weights.

## 7. Paper

Thesis shaped by the rigor pass: *a context compiler with calibrated,
disclosed confidence* — hybrid structural+lexical retrieval (validated
LORO, significant dense-leg gains), an evidence-aware sufficiency signal
(uninformative→r≈0.29 measured on 1,459 cases across two languages), and
per-repo fitted calibration (8/9 held-out repos), with honest nulls
(adaptive blending, component re-weighting) and a measured GT-validity
bound. Venue targets: FSE/ASE/ICSE research track once item 1 exists —
without the downstream eval it's a strong tool paper, with it it's a
retrieval-for-code paper with a calibration story no baseline ships.

What a PC will demand beyond the current evidence, in order:

- **The downstream eval (item 1).** Non-negotiable for a research-track
  claim; every other row here supports it.
- **Published-system baselines (item 4),** not just signal families:
  Aider repo-map head-to-head at equal budgets minimum; positioning
  table vs RepoGraph / CodexGraph / Agentless-style localization; one
  code-tuned embedding model so the dense numbers are not an NL-encoder
  floor.
- **Effect sizes and multiple-comparison handling.** The paired
  permutation tests exist; add a standardized effect size (e.g. Cliff's
  δ) next to every p, and a Holm correction wherever a table reports a
  family of tests (LORO folds, cutoff policies).
- **Threats-to-validity section drafted from measurements, not
  boilerplate:** construct (co-change GT incompleteness — measured, §2),
  internal (LORO for all tuning — done), external (one ecosystem:
  9 Python + 4 TS repos; say so), plus LLM-eval contamination controls
  once item 1 runs.
- **Artifact evaluation readiness:** one-command reproduction (Docker
  image that runs eval_v2 + LORO from pinned SHAs), archived with a DOI
  (Zenodo) at submission time. The pinned-SHA convention is already in
  place; the missing piece is the single entry point.
- **A user-facing case study** (optional but strengthening): one real
  agent loop (e.g. an open coding agent) instrumented with/without
  DiffContext on a fixed task set — this doubles as item 1's
  ecological-validity argument.
