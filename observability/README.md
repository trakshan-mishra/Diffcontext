# observability/ — DiffContext under Neatlogs

Runs DiffContext's five context providers as five comparable traces in
[Neatlogs](https://neatlogs.com). Same task, same budget, same seeds — only the
provider changes, which is exactly the comparison the downstream eval makes,
now visible as span trees with per-arm latency and token counts.

## Setup

```bash
pip install -U neatlogs rank_bm25          # rank_bm25 only for the bm25 arm
```

Get the project API key: **app.neatlogs.com → Quick setup → Open full setup →
Project API key → show → Copy**. Then:

```bash
cp observability/.env.example observability/.env
# paste the key into observability/.env  (already covered by .gitignore)
```

## Run

Index a **copy** of a benchmark repo, so cold/warm timings start from a known
state rather than whatever cache the last run left behind:

```bash
rm -rf /tmp/reqrepo && cp -r benchmark_repos/requests /tmp/reqrepo
rm -f /tmp/reqrepo/.diffcontext_cache.db*

python observability/trace_arms.py --repo /tmp/reqrepo
```

With a real LLM call per arm (adds token/cost spans):

```bash
export GROQ_API_KEY=...        # free tier; or OPENROUTER_/MISTRAL_/OPENAI_/ANTHROPIC_
python observability/trace_arms.py --repo /tmp/reqrepo --llm groq
```

Options: `--seeds`, `--max-tokens`, `--providers`, `--llm`.

## Trace shape

```
WORKFLOW  diffcontext_arm
├── CHAIN      index_repository      → symbols indexed, ms
├── RETRIEVER  rank_symbols          → candidates ranked, top-5, ms
├── CHAIN      pack_token_budget     → kept/dropped, tokens used vs budget
└── LLM        (auto)                → only with --llm
```

`RETRIEVER` is the honest OpenInference kind for the ranking step: it *is*
retrieval over a symbol corpus, just graph-based rather than embedding-based.

## Baseline numbers

`requests` @ 8k budget, seed `./src/requests/sessions.py:Session.request`,
248 symbols indexed in 0.10s:

| arm | ranked | kept | est. tokens | budget used | rank latency (warm) |
|---|---|---|---|---|---|
| `diffcontext` | 247 | 20 | 7,989 | 99.9% | 14.3 ms |
| `diffcontext_gap` | 13 | 13 | 6,284 | 78.5% | 3.1 ms |
| `bm25` | 100 | 18 | 7,938 | 99.2% | 96.4 ms |
| `samefile` | 28 | 23 | 7,954 | 99.4% | 0.6 ms |
| `none` | 0 | 0 | 0 | 0% | 0.2 ms |

Latencies are from a **warm** process (measured off the OTLP wire, see
`otlp_capture.py`). An earlier cold-start run put `bm25` at 355.7 ms, which is
first-call cost — `rank_bm25` import plus corpus construction — not steady-state
ranking cost. Quote the warm number.

**Caveat on that column: it mixes first-call and steady-state cost across arms.**
Arms run in a fixed sequence and each family pays its import + corpus
construction on its first call, so a single-pass run charges that to whichever
arm happens to go first — which is `diffcontext`. Re-running with the order
reversed and with repeats separates the two:

```
python observability/trace_arms.py --repo /tmp/reqrepo \
  --providers bm25 samefile diffcontext_gap diffcontext bm25 diffcontext
```

`rank_symbols`, in execution order:

| position | arm | ms |
|---|---|---|
| 1 | `bm25` | 83.6 |
| 2 | `samefile` | 0.4 |
| 3 | `diffcontext_gap` | 24.7 |
| 4 | `diffcontext` | 4.7 |
| 5 | `bm25` (repeat) | 40.1 |
| 6 | `diffcontext` (repeat) | 4.9 |

Whichever of `diffcontext`/`diffcontext_gap` runs first pays ~24 ms — they share
`_hybrid_ranking`, so the second one is ~5 ms. `rank_bm25` rebuilds its corpus on
every call, so its steady state is ~40 ms on top of ~45 ms of first-call import.

Two things worth a second look:

- **`bm25` is roughly an order of magnitude slower than `diffcontext`** for a
  near-identical token fill. Steady-state that is ~40 ms vs ~4.8 ms (**~8×**);
  single-pass runs read 4×–7× depending on which arm goes first. The strongest
  single baseline is also the most expensive one, and the head-to-head numbers in
  `docs/BENCHMARKS.md` don't surface that. Quote it as an order of magnitude, not
  a fixed constant — and say which regime you measured.
- **`diffcontext_gap` fills only 78.5% of the budget** — it stops at the
  precision cutoff instead of packing to the line. Whether that's a win depends
  entirely on downstream pass rate, which is precisely what `run_eval.py`
  measures and what the LLM arm here will show per-trace.

## Two bugs found while wiring this up — both misdiagnosed at first

Both are fixed. Both are written up with the original diagnosis shown and
corrected, because in each case the first explanation was wrong in a way that
changed what the bug *meant*.

**1. The benchmark harness — not the product — could exceed its own budget.**
The `samefile` arm reported 8,011 estimated tokens against an 8,000 budget.

The cause is in `benchmarks/downstream/providers.py:render_context`, which
accumulated `used += _estimate_tokens(block)` per block but returned
`"\n".join(parts)`. That undercounts twice, not once:

| | tokens |
|---|---|
| sum of per-block estimates | 7,994 |
| actual assembled text | 8,011 |
| budget | 8,000 |

Of the 17-token gap, **7** come from the join separators (23 uncounted `\n`)
and **10** from per-block `int()` flooring — 24 blocks each shedding up to a
token, versus one floor over the whole string. An earlier draft of this file
blamed only the joiner, which accounts for well under half of it.

Fixed by budgeting against the character count the final join will emit and
flooring once. Verified 0 over-budget cases across 5 arms × 6 budgets;
`samefile` at 8k now packs 23 blocks for 7,954 tokens instead of 24 for 8,011.
Two regression tests added, and confirmed non-vacuous by reverting the fix and
watching them fail (`budget=50 produced 53 tokens`).

**Scope: this is an eval-fairness defect, not a product defect.** It never
touched shipped output — `render_context` is the harness's own uniform
renderer, and it had no test coverage despite all five arms depending on it.
The product's packer is a different code path and enforces the budget the
right way: `diffcontext/context/compiler.py:296` re-renders and re-counts
against the *real* output text, dropping symbols until it fits. It overshoots
only at a documented non-compressible floor (meta header + changed symbols),
and when it does, the meta's own token lines report the true count — so the
overshoot is disclosed, never silent.

The practical impact on published numbers is small — 11 tokens on 8,000 is
0.14% — but it mattered here because the eval's entire premise is "hold the
budget fixed, vary only the provider."

**2. Cache symbol collision — right error, wrong cause.**
An earlier draft reported that `Cache.__init__` defaults to a cwd-relative
`db_path`, so indexing a second repo from a directory holding another cache
raises `UNIQUE constraint failed: symbols.id`. On re-examination that is wrong
in its central claim. All three construction sites pass a repo-scoped path —
`diffcontext/parser.py:174`, `diffcontext/pipeline.py:121`,
`diffcontext/pipeline.py:435` — and have since `727e71d` (2026-07-10). The bare
default at `diffcontext/cache.py:53` is unreachable in production.

The stated repro runs clean: `index_repository("benchmark_repos/requests")`
from the repo root, with the 34 MB root cache present, returns 248 symbols and
no error. Re-indexing after modifying a file is also clean.

But the *error* was real, and the mechanism behind it has now been reproduced.
`cache.py` clears stale rows with `DELETE FROM files` plus `ON DELETE CASCADE`,
then inserts symbols keyed on `symbols.id`. Those two only line up while
`Symbol.file` is byte-identical to the `files.file_path` key being deleted. The
Python parser keeps both absolute, so the invariant holds. It breaks when a
symbol is stored under a *different* path form than the one used to index —
which is what a language adapter reporting relative paths would do. Two
outcomes, both confirmed against the real schema:

| stale symbol's `file_path` | result |
|---|---|
| not present in `files` | `FOREIGN KEY constraint failed` — immediate, loud, first pass |
| present in `files` (indexed earlier under that form) | cascade clears nothing, stale row survives, re-index → **`UNIQUE constraint failed: symbols.id`** |

The second row is the reported error, exactly. Reaching it needs the same file
recorded under two path forms — e.g. one indexing pass made from a different
cwd. So the original instinct that *cwd* was involved was pointing at something
real; the `Cache.__init__` default was just the wrong suspect.

Fixed: the insert is now `INSERT OR REPLACE`, which collapses the collision
class regardless of which adapter is loaded. Locked in by
`tests/test_cache.py::test_stale_row_under_other_path_form_does_not_collide`,
which stages the two-path-form condition and asserts one row carrying the new
content. Confirmed non-vacuous — reverting to plain `INSERT` fails it with
`sqlite3.IntegrityError: UNIQUE constraint failed: symbols.id`, the reported
error reproduced in a test. Cold/warm/modify/re-index on `requests`
(248 → 249 symbols) stays clean; suite is 189 passed, 2 skipped.

Still **untested end-to-end**: the TypeScript extra is not installed in this
venv, so `available_adapters()` returns `[]` and no adapter actually exercises
the relative-path path today. Whether a shipped adapter *does* report relative
paths is unverified — the fix does not depend on the answer.

## Neatlogs findings (verified off the wire)

Neatlogs exports over standard OTLP/HTTP, so `otlp_capture.py` stands up a local
receiver, decodes the protobuf, and shows exactly what the backend is sent. Every
claim below was checked against that payload rather than against the dashboard.

```bash
python observability/otlp_capture.py /tmp/cap.json 4318 &
export NEATLOGS_ENDPOINT_FORCE=http://127.0.0.1:4318
python observability/probe_ui_bugs.py
python observability/trace_arms.py --repo /tmp/reqrepo    # arms, off the wire
```

Both scripts honour `NEATLOGS_ENDPOINT_FORCE`. Without it they export to
production — see finding 3, which is why the variable exists at all.

**1. Child spans render out of causal order — and the SDK is not at fault.**
On the wire, `rank_symbols` starts 22 ms before `pack_token_budget`, durations
are correct (18.07 ms vs 2.87 ms), and both carry the same parent span id. The
dashboard shows the reverse, and draws the 3 ms bar wider than the 19 ms one.
That isolates it to rendering. `probe_ui_bugs.py` ships a minimal reproducer
whose three children (300 ms → 20 ms → 150 ms) are ordered so the rendered
sequence identifies what the UI is actually sorting on.

**2. `neatlogs.trace.complete` violates OTel parent/child containment.** This
SDK-internal span is emitted as a child of the root but *starts after the root
has ended* — by +2.2 to +7.7 ms across runs (6 of 16 child spans in a five-arm
run, in every run measured). A child whose lifetime escapes its parent's is a
plausible cause of the timeline mis-layout in finding 1, though that link is
unconfirmed.

**3. `init()` ignores `$NEATLOGS_ENDPOINT`.** The env var is read by
`_wrap_utils._bootstrap_from_env` (the `neatlogs.wrap()` path) but `init()` takes
`endpoint="https://ingest.neatlogs.com"` as a default arg and never consults the
environment — so an endpoint override silently goes to production instead. Note
that `NEATLOGS_API_KEY` *is* read from env, which makes the asymmetry surprising.
Pass `endpoint=` explicitly, as both scripts here now do.

This one has teeth: a run intended for a local receiver reaches
`ingest.neatlogs.com` with no warning, and is rejected only if the key happens
to be invalid. Anyone reproducing these findings against their own capture hits
it, which is why it is worth fixing upstream rather than just documenting.

**4. `capture_input` serializes everything, and it costs real wall-clock.**
`RepositoryIndex` (248 symbols + graph) is re-serialized onto *every* span:
`input.value` is ~330 KB, repeated. Identical 22-span run, measured on the wire:

| content capture | wire bytes | spans | five-arm wall clock |
|---|---|---|---|
| default (on) | 5,424,707 | 22 | 5.64 s |
| `NEATLOGS_TRACE_CONTENT=false` | 13,751 | 22 | 1.57 s |

394× the bytes for the same span tree, and ~4 s of added wall clock on a job
whose real work is ~1.5 s. User CPU is unchanged (1.98 s vs 1.93 s), so the cost
is blocking on upload, not serialization. There is no truncation on the decorator
path and no size guard by default; `NEATLOGS_TRACE_CONTENT=false` is the escape
hatch and is not surfaced in the setup flow.

**5. `@span` on a generator times the wrong thing.** The span closes when the
generator object is constructed, not when it is consumed: 450 ms of real work
reports as 0.1–0.2 ms. This matters well beyond this repo — streamed LLM
responses are generators.

**6. Cosmetic.** `kind="RETRIEVER"` goes out on the wire as
`openinference.span.kind=RETRIEVER` but renders as `RETRIEVAL`. The shutdown
warning `Attempting to uninstrument while already uninstrumented` originates in
upstream `opentelemetry/instrumentation/instrumentor.py`, triggered by the
Neatlogs shutdown path.

**Checked and correct** — worth stating, since these are where tracing SDKs
usually break: thread-pool context propagation, `asyncio.gather` propagation,
6-deep nesting parentage, and exception capture (OTel status 2 plus an
`exception` event carrying type, message, and stacktrace; there are no error
*attributes*, which is easy to misread as "exceptions aren't recorded").

**Environment.** The SDK pins `>=3.10,<3.14`; system Python on current Ubuntu is
3.14, so this needs a 3.13 venv.
