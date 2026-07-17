# Audit remediation summary (2026-07-17)

Six fixes from an external research-grade audit of this repository. Each
entry states what changed, where, and the before/after evidence. Where a gap
could not be fully closed, the remaining limitation is documented instead of
implied away — same convention as the rest of this project.

Full test suite after all changes: **122 passed** (was 103; +19 new tests).

---

## 1. (Critical) `--max-tokens` now bounds the full output

**Defect (reproduced live):** the selector budgeted on `token_count(symbol.code)`
alone while the compiler rendered each symbol with `FILE:`/`FUNCTION:` headers
plus a CALLERS/CALLEES block and prepended a meta header, so the full output
systematically overshot the requested budget by 25–41%.

**Changed:** `diffcontext/context/compiler.py` (per-symbol rendering extracted
into a shared `render_symbol_block()`; post-render trim pass enforces the
budget against the final full output), `diffcontext/context/selector.py`
(budgets candidates at full rendered size, pessimistic `[NOT IN CONTEXT]`
sizing), `diffcontext/pipeline.py` (threads the graph through),
`tests/test_token_budget.py` (new; fails against the previous behavior),
`CHANGELOG.md`.

**Evidence — the audit's exact repro on psf/black
(`compile --changed ./src/black/__init__.py:format_file_contents`, black @
`51abf530`), "Output tokens (full)" vs requested budget. Measurement
discipline note: these are the numbers with ALL fixes in this pass applied,
including the §6 disclosure line (~60 meta tokens at every budget); an
intermediate sweep taken between fix 1 and fix 6 read ~60 lower
(e.g. 8,000→7,269) and briefly appeared in the changelog — corrected there,
with the delta explained rather than papered over. Rerunning at a different
black HEAD will shift symbol content and therefore these numbers; pin the
SHA to compare.**

| Budget | Before | After | |
|---|---|---|---|
| 500 | 1,391 (+178%) | 774 | disclosed floor, see below |
| 1,000 | 2,080 (+108%) | 992 | within budget |
| 2,000 | 3,508 (+75%) | 1,743 | within budget |
| 4,000 | 6,453 (+61%) | 3,660 | within budget |
| 8,000 | 11,268 (+41%) | 7,329 | within budget |

**Remaining limitation (bounded, on purpose, tested):** the meta header (the
disclosure layer) and the changed symbols themselves are never dropped. When
that floor alone exceeds the budget — black at 500: 774 tokens ≈ 416 meta +
358 changed-symbol block — the floor is emitted and the real token count is
visible in the meta's own lines. The overshoot can no longer be silent, and
it can no longer contain any droppable symbol.

## 2. (High) Precision stated in the README headline

**Defect:** the README led with "~2× the recall of grep" while the ~5–10%
precision (92–94% of retrieved symbols not in the co-change ground truth) sat
at the bottom of the benchmark report.

**Changed:** `README.md` only — the top bullet now carries the precision
figure in the same sentence as the recall claim, and the benchmark section
quotes the cross-repo means (0.075 hybrid / 0.060 graph-only) with the
report's own "precision is this product's real problem" framing.
`benchmarks/EVAL_V2_REPORT.md` untouched (it was already honest).

## 3. (High) Dense-retrieval baseline added to the benchmark

**Defect:** the benchmark compared five methods, none of them the
embedding-based retrieval most 2026 code-RAG tooling actually runs.

**Changed:** `benchmarks/baselines.py` (`EmbeddingBaseline`),
`benchmarks/eval_v2_hardened.py` (sixth method in the same loop: identical
per-commit statistics, bootstrap CIs, stratification, failure buckets; every
summary now also records the repo HEAD SHA and which encoder ran),
`benchmarks/check_regression.py` (documented decision: baselines get no CI
floors), `benchmarks/EVAL_V2_REPORT.md` (methodology + new §8 with the full
six-method re-run).

**Honest limitation of this run:** the baseline prefers
sentence-transformers/all-MiniLM-L6-v2, but the environment this re-run
executed in blocks huggingface.co downloads, so it fell back to the
explicitly-labeled `tfidf-cosine-approx` encoder — a lexical-vector
approximation, **not** true dense retrieval. The results below are therefore
a second, stronger lexical baseline; a true dense run is still an open item
and the harness records the encoder per-run so the two can never be
conflated.

**Results (full six-method re-run on pinned SHAs, `EVAL_V2_REPORT.md` §8;
n=424 valid commits on this snapshot vs the frozen tables' 423 — repo HEADs
moved between runs, so one more django commit survived mining; this is why
runs are now SHA-pinned):**
the new baseline beats BM25 on recall in **5/5 repos** (cross-repo mean
0.664 vs 0.624), making it the strongest single baseline tested. The
shipped hybrid still leads in 4/5 repos (mean recall 0.693, R@20 0.629 vs
0.585) — but **loses to the embedding baseline outright on pydantic**
(0.524 vs 0.561 recall), where metaclass-generated code blinds the call
graph. That unflattering result is stated as such in the report, and it is
the measured case for the adaptive-blend roadmap item. In the django
failure buckets the baseline also recovered 3/20 cross-subsystem pairs
where graph, BM25, and hybrid all score 0/20 (weak evidence at n=20, noted
as such).

## 4. (Medium) Language-scope claim corrected

**Defect:** roadmap claimed "the architecture is language-agnostic; only
parser.py/graph_builder.py are Python-specific" — but those two files are all
of parsing, symbol extraction, and graph construction.

**Changed:** `README.md` roadmap item 6 now states: Python only today; a
second language requires a parser and graph builder built from scratch for
that language's AST; for any JS/TS/Go/Rust/Java repo the tool currently
retrieves nothing. (The only other "language-agnostic" phrase in the docs,
in `docs/VERIFY.md`, already correctly scoped itself to the JSON case format
and states "the parser/graph is Python-only" — left as is.)

## 5. (Medium) `/clone` endpoint hardened

**Defect:** `POST /clone` accepted any URL with no private-address check, no
size cap beyond `--depth=1`, no rate limit, no auth.

**Changed:** `diffcontext-service/backend/main.py`:
- hostname resolved **before** cloning; loopback/link-local/RFC1918/
  reserved/multicast/unspecified addresses rejected (one private record
  among several is enough to reject);
- post-clone size cap, default 500MB (`DIFFCONTEXT_MAX_CLONE_MB`), with
  cleanup on rejection — checked post-clone because `--depth=1` bounds
  history, not blob size;
- per-IP rate limit (default 5/10min; `DIFFCONTEXT_CLONE_RATE_LIMIT`,
  `DIFFCONTEXT_CLONE_RATE_WINDOW_S`), in-memory and single-instance by
  design with the replacement seam documented;
- `diffcontext-service/README.md` gained "Security notes for self-hosters"
  stating what is enforced **and what is not**: no auth by default, DNS
  rebinding not closed (git doesn't expose IP pinning), rate limiting is
  proxy-blind behind a reverse proxy.

**Tests:** `tests/test_service_clone.py` (14 tests): private-IP rejection
incl. mixed public+private DNS answers and `git@` URLs, oversized-clone
rejection with cleanup verification, rate limiting, and a mocked end-to-end
pass for a legitimate public URL. The module skips cleanly when FastAPI
isn't installed, so core CI is unaffected.

## 6. (Medium) Structural ceiling disclosed in the CLI output

**Defect:** cross-subsystem conceptual co-changes score 0/20 recall for
every static method — disclosed deep in the benchmark report while the CLI
printed an unqualified "Graph confidence: 100%".

**Changed:** `diffcontext/context/compiler.py` — the meta-header now carries
a permanent line stating graph confidence measures *structural* completeness
only and conceptually-coupled code may exist unlisted; it is part of the
fixed disclosure block, never compacted under any budget (tested at budgets
down to 60 tokens, `tests/test_compiler_meta.py::TestStructuralCeilingCaveat`).
`docs/VERIFY.md`'s honesty contract now explains the same ceiling in full,
with the 0/20 number and why a perfect sufficiency score is consistent with
a missed conceptual partner.

---

## What remains open (not claimed as fixed)

- **True dense-embedding numbers** (§3): the code path exists and
  self-labels, but this run's numbers are the TF-IDF fallback. Running once
  in an environment with huggingface.co access completes it.
- **Independent replication** (audit finding 4): re-run pinning (HEAD SHA +
  encoder now recorded per summary) makes replication *possible*; nobody
  independent has done one yet.
- **PyPI packaging / project.urls / service lockfile** (audit finding 8):
  not addressed in this pass.
- **The 500-token floor** (§1): outputs below ~800 tokens on mid-sized repos
  cannot be guaranteed while the meta header and changed symbols are
  undroppable; the floor is disclosed in-band instead.
