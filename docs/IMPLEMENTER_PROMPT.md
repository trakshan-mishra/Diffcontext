# Implementer prompt — `diffcontext verify` (build #1 from the roadmap)

Copy everything in the block below into a fresh coding-agent session, run
from the repo root. It is scoped to ONE capability so it can't sprawl.

---

You are working in the DiffContext repository (a static-analysis code-context
compiler for LLMs; pure-stdlib, Python 3.8+, 85 passing tests in `tests/`).
Read `README.md`, `diffcontext/pipeline.py`, `diffcontext/context/compiler.py`,
and `diffcontext/graph_builder.py` before writing any code.

## Task
Add a `diffcontext verify` CLI command and a `verify_context()` library
function that audits whether a compiled context is SUFFICIENT and not padded
with low-value symbols. No LLM calls — this must be deterministic and offline.

## Exact behavior
`verify_context(index, impact, package)` returns a `VerifyReport` dataclass:
- `sufficiency: float` in [0,1] — fraction of the changed symbol's
  DIRECT graph neighbours (callers + callees) that are present in the
  compiled context. 1.0 means every direct neighbour made it in.
- `blind_spots: List[str]` — repo symbols whose source text contains a call
  to the changed symbol's short name (`name(`), that are NOT in the context
  AND NOT in `package.dropped_symbols`. These are true undisclosed omissions.
- `low_value: List[str]` — included non-changed symbols with score below a
  threshold (default 20) whose removal would not disconnect any other
  included symbol from the changed one. Candidate padding.
- `verdict: str` — "sufficient" if sufficiency>=0.8 and blind_spots empty;
  "review" otherwise.

CLI: `diffcontext verify --changed SYMBOL [--repo .] [--max-tokens N]`
prints the report human-readably and exits 0 for "sufficient", 1 for "review"
(so it can gate CI).

## Constraints
- No new runtime dependencies. Reuse existing graph/reverse-graph helpers.
- Do NOT modify scoring or selection behavior — this only READS a package.
- Add `VerifyReport` to `diffcontext/__init__.py.__all__`.

## Tests you MUST add (tests/test_verify.py) and make pass
1. A fixture where the context includes every direct neighbour → sufficiency
   == 1.0, verdict "sufficient".
2. A fixture with a caller of the changed symbol deliberately excluded and
   NOT in dropped_symbols → it appears in `blind_spots`, verdict "review".
3. A fixture with a genuinely irrelevant included symbol → it appears in
   `low_value`.
4. Determinism: two runs on the same input produce identical reports.
5. The existing suite still passes (run `python -m pytest tests/ -q`).

## Definition of done
- `python -m pytest tests/ -q` all green (existing 85 + new).
- `diffcontext verify --changed ./src/black/__init__.py:format_file_contents`
  run against a cloned black repo prints a report and exits with a code.
- Update README's "What you get back" section with a short `verify` example.
- Update CHANGELOG under [Unreleased].

Do not implement session state, provenance, or feedback — those are separate
future builds. Stay scoped to verify.
