# Language support & adapters

Full detail on what each language adapter resolves, how it was measured,
and where it fails. Summary table in the [README](../README.md#language-support).

| Language | Status | How | Retrieval quality |
|---|---|---|---|
| Python | **Full** | stdlib `ast`, deep resolver ([ARCHITECTURE.md](ARCHITECTURE.md)) | Benchmarked: 423 commits, 5 repos + 2 validation repos ([BENCHMARKS.md](BENCHMARKS.md)) |
| TypeScript / JavaScript (ESM) | **Working prototype** | tree-sitter adapter, `pip install -e ".[typescript]"` | Measured on 4 repos (below): mean recall **0–68% depending on code style** — not one number |
| JavaScript (CommonJS) | **Effectively unsupported** | `require()`/`exports.x =` not resolved | Measured 0.0% on express — do not use on CJS repos |
| Go / Rust / Java / others | Not supported | — | Retrieves nothing |

Without the `[typescript]` extra installed, DiffContext is exactly the
Python-only tool: no behavior change, no warnings.

## TypeScript/JavaScript: measured results

Mined co-change cases (`verify --from-history 25`, the same harness you can
run on your own repo — Python's mined-case baseline on django is 58.6%
for comparison):

| Repo | Shape | Cases passed | Mean recall | Why |
|---|---|---|---|---|
| honojs/hono | ESM TS framework | 19/25 | **67.9%** | Clean relative-import graph |
| colinhacks/zod | TS monorepo, type-heavy | 16/25 | **58.3%** | Chained-generic style limits receiver typing |
| sindresorhus/ky | Small ESM TS lib | 6/25 | **34.5%** | History dominated by one mega-commit; cross-file type↔impl spread |
| expressjs/express | CommonJS JS | 0/19 | **0.0%** | CJS: `exports.x = function` yields almost no symbols |

Read that table as the finding it is: **retrieval quality tracks code
style, not language**. ESM TypeScript with a clear import graph lands in
the same band as Python; CommonJS is a named, measured failure mode.
These are mined-case smoke signals, not the five-repo benchmark
methodology — that has not been applied to TS yet.

## What the TS adapter resolves

Functions, class methods, arrow consts, namespaces, ES imports
(named/default/namespace, aliases, barrel `index.ts` re-exports incl.
`export * from`), tsconfig/jsconfig `baseUrl` + `paths` aliases
(`@services/*`), `this.method()`, `super()`, `new Class()`, `extends`
override edges, function references passed as arguments — and
**declared-type resolution**: parameter/field/local annotations and
`new X()` inference make `u.login()`, `this.db.query()` resolve to the
right class method, and every interface/type-alias a signature mentions
gets a consumer→type edge (editing `types/options.ts` pulls its consumers
into the blast radius, the TS-specific co-change pattern call graphs
can't see).

Still unresolved, disclosed: untyped receivers, tsconfig `extends`
chains, CommonJS.

## The `verify` score on TypeScript (fixed, measured)

The score used to have **zero** discriminating power on TS: components
with no observations behind them (no direct neighbors, no outgoing
edges — routine in a sparse TS graph) defaulted to a perfect 1.0, so
every case scored ~100 regardless of measured recall. Measured at scale
(360 mined cases across hono/zod/ky, `benchmarks/calibration_at_scale.py`):
the legacy formula scored **μ=99.2, σ=5.0, r=0.02 with recall (nothing)**.

The score is now **evidence-aware**: it shrinks toward 50 ("don't know")
in proportion to missing evidence, and the report prints the evidence
fraction. Same 360-case measurement after the fix: **μ=81.0, σ=17.3,
pooled r=0.29 (p=0.0001)** — real discrimination, though per-repo
strength varies (ky r=0.42; hono/zod n.s.). CommonJS express, where the
adapter extracts almost nothing, now honestly reports ~55 low-evidence
instead of a confident 100.

The same measurement showed the legacy formula was equally uninformative
on *Python* at scale (r=0.016, n=1080) — this was never a TS-only bug,
TS just made it obvious. For actual confidence, calibrate on your repo:
`diffcontext verify --from-history 60 --calibrate --save-calibration`.

## Exclusion policy (why installing the extra is safe)

Vendored/static JS inside Python repos (django's admin jquery, for
example) is excluded by an adapter-level policy (`static/`, `vendor/`,
`*.min.*`, colocated `*.test.ts`/`*.spec.ts`), so installing the extra
does not pollute existing Python indexes.

## Adapter roadmap, in order of measured need

1. **CommonJS support** — `require()`, `exports.x =`; the measured 0%
   failure mode
2. **Per-language hybrid blend weights** — current weights were tuned on
   Python graph density
3. **Apply the five-repo benchmark methodology to TS**

(TS-aware sufficiency was item 1; done via the evidence-aware score —
see the section above for the before/after measurement.)
5. **Further adapters** (Go/Rust) via the `diffcontext/languages/`
   template
