# Your Coding Agent Doesn't Know What It Can't See. DiffContext Tells It.

*An open-source static analysis tool that picks the code an LLM actually needs to see for a given change, packs it into a hard token budget, and, critically, discloses exactly what it left out.*

---

If you build LLM-powered dev tools, you have hit this problem: a user asks the model to change one function in a 50,000-line repository. What code do you put in the context window?

You have three options today, and all of them are bad. You can paste the whole repo, which usually doesn't fit, and even when it does, models get measurably worse at finding things inside huge piles of irrelevant context. You can paste just the changed function, and watch the model confidently propose a fix that breaks three callers it never saw, because it couldn't see the callers, the callees, or the sibling function that must change in lockstep. Or you can grep for the function's name and paste the matches, which is better, but grep only finds code that literally mentions the name. The subclass that overrides your function, the handler that receives it through `functools.partial`, the config check that always changes together with it: none of those necessarily contain the string you grepped for.

That last failure is not hypothetical. We measured it. Grep's recall plateaus no matter how much token budget you give it. Past roughly 4,000 tokens, more budget buys grep nothing. The numbers are below.

[DiffContext](https://github.com/trakshan-mishra/Diffcontext) is a fourth option: parse the repository once into an AST-derived call graph, then for any changed function, rank every other symbol in the repo by how likely it is to matter, and compile the top results into a hard token budget.

## What it does differently

DiffContext statically parses every Python file in the repo once and builds a call graph: who calls whom, who inherits from whom, who decorates whom, plus function references passed as arguments (`functools.partial(fn, ...)`, `sorted(xs, key=fn)`), which are real dependencies even though nothing "calls" them at that site.

For a changed function, it then blends three signals to rank candidate context:

- **Call-graph distance** (weight 0.5): callers might break, callees explain behavior
- **BM25 lexical similarity** (weight 0.35): catches related code with no call edge
- **Same-file co-location** (weight 0.15): weak, cheap, catches what the other two miss

Each signal alone has a measured blind spot, and the blend beats all three individually (numbers in the next section). The top-ranked symbols get packed into whatever token budget you specify.

## The part that actually matters: the honesty header

Ranking and packing is table stakes. Every retrieval system does some version of it. The thing DiffContext does that I have not seen elsewhere is this: the compiled output leads with a meta-header stating exactly how many symbols exist in the repository, how many made it into context, and how many were dropped. And every dropped symbol is named, with its score, not silently hidden.

```
=== DIFFCONTEXT META ===
Repo symbols total    : 648
Symbols IN context    : 18
Symbols DROPPED       : 630  <- you cannot see these
...
DROPPED SYMBOLS (630) - scored but cut by token budget:
  - ./src/black/linegen.py:transform_line  (score: 71)
  ...
```

Inside the body, every function is annotated with its callers and callees, and anything referenced but not included is tagged `[NOT IN CONTEXT]`.

Why spend tokens on this? Because an LLM that doesn't know what it can't see will confidently hallucinate about code it never saw. The header gives the model the ability to distinguish "this function doesn't exist" from "this function exists but wasn't shown to me." The first justifies writing new code; the second justifies asking for it or flagging uncertainty. Without the header, both look identical to the model.

We stress-tested this claim. At a deliberately tight 2,000-token budget, an audit showed that 0% of the ground-truth functions DiffContext failed to include were silently invisible. Every single miss was disclosed in the dropped manifest.

## The numbers

Everything below is measured, not claimed. The benchmark runs on 423 real commits across django, flask, click, httpx, and pydantic, using each commit's actual co-changed functions as ground truth: if you change function A, did the retriever surface the other functions that the real developer changed in the same commit?

| Method | Hit rate | Recall |
|---|---|---|
| Same file only | 0.69 | 0.51 |
| Call graph only | 0.75 | 0.56 |
| BM25 only | 0.82 | 0.62 |
| **Hybrid (DiffContext)** | **0.86** | **0.69** |

Head-to-head against grep at identical token budgets, on 30 real co-change queries drawn from black's commit history: at 8,000 tokens, grep plateaus at 0.215 recall while DiffContext reaches 0.576, a 2.7x gap. Grep stops improving past roughly 4k tokens no matter how much budget you give it. DiffContext keeps climbing, because the call graph reaches code that never mentions the query string.

To guard against overfitting to the benchmark repos, we also ran independent validation on repos never used for tuning (black, requests): hit rate 0.90 to 0.97, recall 0.72 to 0.77.

Speed matters too, since agents re-index constantly. Re-indexing an unchanged repo takes about 0.02s from the content-addressed cache. A one-file edit takes about 0.4 to 0.6s as an incremental update, versus 1.6 to 4s for a full re-parse (measured on pydantic, 1,830 symbols).

And the numbers stay honest going forward: the benchmark is CI-gated. Every push re-runs it, and the build fails if retrieval quality regresses.

## How it works, and how to try it

One pass over the repo parses every file into an AST, extracts every function and method as a symbol, resolves imports (including `src/` layouts and re-exports through `__init__.py`), and builds the call graph. The graph is cached content-addressed in SQLite, so only edited files are re-parsed. At query time, a git diff (or a symbol you name) seeds the three-signal ranking, and the compiler packs winners into your budget with the meta-header on top. Zero runtime dependencies, Python 3.8+, MIT licensed, 103 tests.

```bash
git clone https://github.com/trakshan-mishra/Diffcontext.git
cd Diffcontext && pip install -e .
diffcontext index /path/to/project
diffcontext compile --changed ./src/auth.py:validate_jwt --max-tokens 8000
diffcontext compile --ref HEAD~1 --json   # from a git diff, for agents
```

It is not on PyPI yet; install from source as above.

## Known limitations, measured

Static analysis has a ceiling, and it is worth being precise about where it is.

Purely structural relatedness with no call-graph connection (two functions implementing the same feature that never call each other) is only partially recovered by the BM25 leg. Worse, cross-subsystem conceptual links, like a settings flag and the security check that reads it, scored 0/20 for every method we tested. That is a real ceiling for any static-analysis retriever, and the only path we see to it is a future git co-change signal: functions that historically change together, regardless of structure.

Dynamic dispatch (`getattr(obj, name)()`) and metaclass-generated code are statically unresolvable. No parser can follow them, and DiffContext doesn't pretend to.

## What's next

Three things are on the roadmap: an adaptive blend that adjusts the three weights per query instead of using fixed global ones, git co-change history as a fourth signal to break through the conceptual-link ceiling described above, and TypeScript support.

If you are building an agent harness and tired of choosing between "paste everything" and "paste too little," try it on a repo you know well and see what it surfaces. The code, the benchmark harness, and every number in this article are here:

**https://github.com/trakshan-mishra/Diffcontext**

Issues and PRs welcome. If the numbers don't reproduce on your repos, I want to know that most of all.
