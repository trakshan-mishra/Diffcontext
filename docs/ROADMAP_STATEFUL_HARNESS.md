# DiffContext → Stateful, Auditable, Self-Verifying Context for Agent Loops

The uniqueness thesis, the architecture to get there, and how to see each
piece work live. Written to be handed to an implementer (see the prompt in
`FABLE5_PROMPT.md`).

## The unclaimed niche (why this isn't "another PageRank")

PageRank and GraphQL won *general* problems (rank the web; type any API).
Competing with them on generality is a losing game. The gold-tier niche
that is currently UNSOLVED is narrow and specific:

> Stateful, auditable, self-verifying context selection for an agent loop
> that edits the same repo dozens of times per session.

Every existing tool (Aider repo-map, Cursor/Continue embeddings, SWE-agent)
is **one-shot, black-box, and stateless**: it re-derives relevance from
scratch every turn, can't tell you what it deliberately excluded, and never
learns from whether its context actually led to a correct patch.

DiffContext already has three things none of them combine:
- an **incremental index** (`index.update()`) — the foundation for state
- **honest exclusion disclosure** (`dropped_symbols`) — the foundation for audit
- **content-addressed caching** — the foundation for cheap repeat calls

The roadmap below turns those foundations into the four things the niche
needs and nobody ships.

## Target architecture

```
                    ┌──────────────────────────────────────────────┐
                    │              SESSION (new, stateful)          │
                    │   tracks: what the agent has already seen,    │
                    │   token budget spent, per-symbol "staleness"  │
                    └───────────────┬──────────────────────────────┘
                                    │
   turn N: "agent edited auth.py"   │
                                    ▼
 ┌──────────┐   ┌───────────┐   ┌───────────────┐   ┌──────────────────┐
 │  INDEX   │──▶│  IMPACT   │──▶│  SELECT+COMPILE│──▶│  VERIFY (new)     │
 │ .update()│   │  (hybrid) │   │  DELTA only:   │   │ ablation + noise  │
 │ 1 file   │   │           │   │  skip symbols  │   │ → sufficiency     │
 │          │   │           │   │  already shown │   │   score           │
 └──────────┘   └───────────┘   └───────┬────────┘   └────────┬─────────┘
                                        │                     │
                                        ▼                     ▼
                                ┌──────────────────────────────────────┐
                                │  PROVENANCE LOG (new, append-only)    │
                                │  query · scores · dropped · budget ·  │
                                │  sufficiency · outcome (if reported)  │
                                └───────────────────┬──────────────────┘
                                                    │
                                   turn N+1 outcome │  FEEDBACK (new)
                                   "patch failed"   ▼
                                ┌──────────────────────────────────────┐
                                │  builds a real failure dataset →      │
                                │  the only path to scoring that        │
                                │  LEARNS instead of staying hand-tuned │
                                └──────────────────────────────────────┘
```

## The four new capabilities, in build-priority order

### 1. `diffcontext verify` — self-audit (highest differentiation, ~1 session)
Answers "is this context garbage or sufficient?" as a PRODUCT feature, not a
manual benchmark. Two mechanical tests, no LLM required for the cheap version:
- **Ablation**: for each included symbol, note whether removing it drops the
  graph-connectivity / lexical-coverage of the changed symbol. Symbols whose
  removal changes nothing are flagged as low-value padding.
- **Coverage gap**: cross-check the included set against a cheap independent
  signal (grep for the changed symbol's name across the repo). Any grep hit
  NOT in the context and NOT in `dropped_symbols` is a true blind spot —
  report it. (This is the "am I missing useful code" check, automated.)
Output: a `sufficiency` score + an explicit "possible blind spots" list.

### 2. Session-aware delta context — the "loop engineering" core (~1-2 sessions)
`diffcontext session start` → subsequent `compile` calls return only what the
agent has NOT already seen this session (or what changed since it saw it).
Turn 1 costs full budget; turn 5 costs the delta. This is the single feature
that makes it a *loop* primitive rather than a *query* tool.

### 3. Provenance log — auditability (~half a session)
Every `compile` optionally appends a structured JSON record. Answers, after a
bad agent patch: "what did the model see, and what was it never shown?"
This is a compliance/debugging necessity for deployed agents, and nobody
has it.

### 4. Feedback loop — scoring that learns (~2 sessions, needs #3)
`diffcontext feedback --insufficient <symbol>` records that a dropped symbol
was actually needed. Over time this is a labeled dataset that can retune the
0.5/0.35/0.15 blend from real outcomes instead of one-time hand-tuning.

## How to see each piece work live

| Capability | Live proof (run it, watch it) |
|---|---|
| verify | `diffcontext verify --changed X` on black → reports a real grep-hit blind spot you can confirm by hand |
| session delta | two `compile` calls in one session on the same symbol → second returns fewer tokens, prints "N symbols already shown this session" |
| provenance | tail the log after 3 compiles → 3 structured records with dropped lists |
| feedback | feed one `--insufficient`, re-run, watch that symbol's score rise |

## Honest scope boundary (put this in the README, it builds trust)

DiffContext helps when: repo is above ~a few-thousand-token crossover, the
change has real cross-file structure, and the model can't hold the whole
repo. It does NOT help (and says so) on: tiny repos, isolated
single-function changes with no callers, or purely thematic links with no
call-graph or lexical signal (the measured 0/20 cross-subsystem bucket).
