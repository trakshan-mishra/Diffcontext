# Project status

*Every statement below was checked against the source tree on 2026-08-05, not
copied from prose. Where a fact has a location in the repository, that location
is named so it can be re-checked.*

## Install and run it today

The package is `diffcontext`, `diffcontext.__version__` is `0.3.0`
(`diffcontext/__init__.py`), licensed MIT, Python 3.9+ (`pyproject.toml`), and
the core package has **zero runtime dependencies** — it is stdlib only.

Publication to PyPI is wired up (`.github/workflows/publish.yml`, trusted
publishing on `v*` tags) but has not run for the packaged versions: as of
2026-08-05 `pip install diffcontext` does not resolve. Install from source:

```bash
git clone https://github.com/trakshan-mishra/Diffcontext.git
cd Diffcontext
pip install -e .                  # core, stdlib only
pip install -e ".[typescript]"    # optional TypeScript/JavaScript adapter (tree-sitter)
pip install -e ".[dev]"           # test extras

# or, without a working copy (verified 2026-08-05):
pip install git+https://github.com/trakshan-mishra/Diffcontext.git
```

This installs one console script, `diffcontext` (`pyproject.toml`
`[project.scripts]`), with six subcommands: `index`, `impact`, `diff`,
`compile`, `blast`, `verify`.

## The four ways people actually use it

**1. Fit check first — 5 minutes, and it may tell you to stop.**

```bash
cd /path/to/your/repo
diffcontext verify --from-history 30 --calibrate
```

This mines real co-change cases from *your* git history, grades retrieval
against them, and prints `NULL RESULT` rather than a decorative number when the
tool does not fit the repo. Recall well above 0.5 with most cases passing means
the recipes below behave roughly as documented; recall near zero means stop
(common causes: CommonJS, heavy dynamic dispatch, unsupported languages). Add
`--save-calibration` to persist a fitted recall predictor to
`.diffcontext-calibration.json` so later `verify` runs report calibrated
confidence instead of a bare score.

**2. From the command line, around an edit.**

```bash
diffcontext index /path/to/project                                    # cold: seconds; warm re-index ~0.02s
diffcontext blast   --changed ./src/auth.py:validate_jwt              # who is affected?
diffcontext compile --changed ./src/auth.py:validate_jwt --max-tokens 8000
diffcontext compile --ref HEAD~1                                      # start from a real git diff
diffcontext compile --changed ./src/auth.py:validate_jwt --json       # machine-readable
```

Symbol IDs are always `./relative/path.py:ClassName.method` — no parentheses,
no arguments. `compile` defaults: `--depth 2`, `--max-tokens 10000`,
`--top-k 20`, `--cutoff topk`. Add `--cutoff gap` if you pay per token,
`--graph-only` for structural certainty with no lexical blend, `--with-history`
to blend git co-change as a fourth signal.

**3. From a Python agent harness, on every loop iteration.**

```python
from diffcontext.pipeline import index_repository, analyze_impact, compile

idx = index_repository(repo)              # cold: full parse + graph build
idx.update(["src/auth.py"])               # after an edit: re-parses only that file
impact = analyze_impact(idx, ["./src/auth.py:validate_jwt"])
ctx = compile(idx, impact, max_tokens=8000, token_counter=my_tokenizer)

for item in ctx.items:                    # structured, not just a string
    print(item.symbol_id, item.role, item.score, item.token_estimate)
```

The semver-covered surface is the `__all__` list in `diffcontext/__init__.py`:
`BlastResult`, `CoChangeIndex`, `ContextItem`, `ScoringConfig`, `blast_radius`,
`index`, `diff`, `compile_context`, `__version__`. Everything else is importable
but carries no stability guarantee.

**4. As a CI gate.**

`diffcontext verify` exits `0` only on the `SUFFICIENT` verdict (and, with
`--cases`, only when every case passes), so a workflow step can block a merge
whose change lacks the context an agent or reviewer would need.

## What is shipped

- **Hybrid retrieval** blending call graph, BM25 lexical similarity and
  same-file co-location. The shipped weights are `(0.3, 0.5, 0.2)` in
  `(graph, BM25, same-file)` order — `HYBRID_WEIGHTS` in
  `diffcontext/pipeline.py`, the leave-one-repo-out-validated values from the
  2026-07 rigor pass. The originally shipped `(0.5, 0.35, 0.15)` was
  graph-overfit and was retracted.
- **Adaptive blend**, on by default: when the blast radius produced fewer than 8
  candidates the graph's weight is scaled down by graph confidence and the freed
  weight moves to BM25. On well-connected changes the weights are exactly the
  frozen blend. Measured effect on held-out benchmark metrics: **null**
  (p=1.000) — it is shipped for the sparse-graph case, not as a win.
- **Git co-change history as an opt-in fourth signal** (`--with-history`,
  `diffcontext.CoChangeIndex`) — the only signal family that reaches related
  code with no structural or lexical connection.
- **Dispatch-sibling override edges** in the graph builder, including the
  duck-typed case where the base class never defines the method.
- **`--cutoff gap`** on `compile` and `verify`, plus a `precision_lb` column in
  verify results, so the precision/recall tradeoff is measurable per repo.
- **Content-addressed SQLite cache**: re-indexing an unchanged repo ~0.02s; a
  one-file edit re-parses only that file.
- **Honest output**: the meta header discloses the symbols that were scored and
  then dropped, and anything referenced but not included is tagged
  `[NOT IN CONTEXT]`.

## What is measured, and what that does not cover

- Retrieval quality is benchmarked on **423 real commits** across django,
  click, flask, httpx and pydantic, with frozen-weight validation on black,
  requests, rich and starlette. Numbers and methodology: the Benchmarks page,
  `benchmarks/EVAL_V2_REPORT.md`, and `benchmarks/RIGOR_REPORT_2026-07.md`.
- **Precision, not recall, is the open problem**: cross-repo mean precision is
  under 0.1 at the default top-k.
- The `verify` sufficiency score is a **structural proxy**, not a probability,
  and it has **zero discriminating power on TypeScript** today.
- **The downstream question is still open.** Everything above measures whether
  the right code was retrieved (a proxy), not whether an LLM given that context
  produces a better patch. The harness for that exists and has run; the current
  result is a null from a task set that cannot discriminate between arms — on
  the corpus audited 2026-08-05 all three arms scored 0.333 with
  `discrimination: 0/9` and `n_eff=0`. A null from a non-discriminating task set
  is indistinguishable from a null from a retriever that does not help, so no
  claim is made in either direction. Read the `discrimination:` line before any
  pass-rate table from that harness; pooled numbers recorded before 2026-08-05
  were paired on the bare commit rather than `(model, commit)` and are void.

## Quality gates in CI

Every push runs (`.github/workflows/test.yml`): ruff and mypy over
`diffcontext/`; the test suite on Python 3.9, 3.11, 3.12 and 3.13 (**189 tests
collected** at the time of writing); the harness tests again with benchmark
dependencies installed; `benchmarks/check_regression.py` against a cloned flask,
which fails the build if hit/recall drop below frozen floors; and a wheel-scope
check that the built wheel contains only the package and includes `py.typed`.

## Language support, as measured

| Language | Status | Measured |
|---|---|---|
| Python | Full | 423 commits, 5 benchmark repos + 4 validation repos |
| TypeScript / JavaScript (ESM) | Working prototype, optional extra | Mean recall 0–68% depending on code style: hono 67.9%, zod 58.3%, ky 34.5% |
| JavaScript (CommonJS) | Effectively unsupported | 0.0% on express — a named, measured failure mode |
| Go, Rust, Java, others | Not supported | Retrieves nothing |

Without the `[typescript]` extra installed, behaviour is identical to the
Python-only tool.
