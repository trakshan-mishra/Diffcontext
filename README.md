# DiffContext

Static-analysis-powered repository context compiler for LLMs.

**Git diff + AST parsing + dependency graph + blast radius + impact scoring → optimized context package**

Instead of dumping an entire codebase (or doing keyword/vector search), DiffContext:

1. **Parses** Python files via AST → extracts every function, method, class
2. **Builds a dependency graph** → calls, imports, inheritance, attribute ownership, decorators
3. **Detects changes** → via git diff (including uncommitted edits) or snapshot comparison
4. **Computes blast radius** → everything transitively affected by the change
5. **Scores impact** → prioritizes symbols by structural importance
6. **Selects relevant code** → respects a token budget
7. **Compiles context** → structured output ready to paste into an LLM

On a real ~1,100-symbol production repo, this reliably produces **95–99% token
reduction** versus pasting the whole codebase, while keeping the functions that
are actually call-graph-connected to your change.

## Quick Start

### Step 1: Install

```bash
git clone https://github.com/trakshan-mishra/Diffcontext.git
cd Diffcontext
pip install -e .
diffcontext --help
```

### Step 2: Index a repository

```bash
diffcontext index /path/to/any/python/project
```

```
Symbols : 354
Edges   : 160
Time    : 548ms
Files   : 20
```

If any file fails to parse (a real `SyntaxError`), it's reported explicitly —
not silently skipped:

```
Skipping broken_file.py due to SyntaxError: unmatched ')' (line 237)
```

### Step 3: Check impact of a function you're working on

This is the recommended default mode while actively editing — it doesn't
depend on git at all, so it works on uncommitted or untracked files too:

```bash
diffcontext blast --changed ./src/auth.py:validate_jwt
```

Shows who calls it, what it calls, and the full transitive blast radius.

### Step 4: Auto-detect changes from git (optional)

```bash
diffcontext diff
```

Compares your working tree (including uncommitted edits to **tracked** files)
against `HEAD~1` by default. Note: this only sees changes to files git
already knows about — a brand-new untracked file is invisible to any
git-diff-based tool until you `git add -N <file>` or commit it. Use
`--committed-only` to compare two commits and ignore working-tree changes.

### Step 5: Build LLM-ready context

```bash
# From a specific function:
diffcontext compile --changed ./src/auth.py:validate_jwt

# From git diff:
diffcontext compile --ref HEAD~1

# With a token budget:
diffcontext compile --changed ./src/auth.py:validate_jwt --max-tokens 8000

# JSON output (for piping into another tool):
diffcontext compile --changed ./src/auth.py:validate_jwt --json
```

Then paste the output into Claude / ChatGPT / your LLM of choice, **with a
specific question** — not just the raw context. E.g.:

> "Is the dynamic SQL construction in `update_run` safe, given how `kwargs`
> is validated against `_UPDATABLE_RUN_COLUMNS`?"

### Step 6: Use as a library

```python
from diffcontext.pipeline import index_repository, analyze_impact, compile

idx = index_repository("/path/to/repo")
impact = analyze_impact(idx, ["./src/auth.py:validate_jwt"])
ctx = compile(idx, impact, max_tokens=10000)

print(ctx.text)             # the context to send to the LLM
print(f"{ctx.token_estimate:,} / {ctx.total_repo_tokens:,} tokens")
print(f"{ctx.reduction_pct:.1f}% reduction")
```

See `USAGE.md` for the full day-to-day workflow, including shell aliases.

## What the resolver actually handles

Confirmed via an automated test suite (`tests/`) that builds small repos on
the fly and asserts on real resolved call-graph edges — not just "it ran
without crashing":

- Function and method calls, including multi-hop attribute chains (`self.a.b.method()`)
- Multiple inheritance / MRO, including cross-file base classes
- Circular imports
- Local variables instantiating a class inside a **free function** (not just
  `self.x = ...` inside a method) — e.g. `h = Handler(); h.process()`
- Annotated parameters as call receivers (`def run(h: Handler): h.process()`)
- Import aliasing (`from .user import Handler as UserHandler`), including
  disambiguating two same-named classes in different files
- Bare `import x` where `x` lives in a sibling directory rather than the
  repo root (common in script-style codebases)
- **Decorators**: a decorated function's graph entry now correctly includes
  calls made by its decorator's wrapper — e.g. `@require_auth` wrapping
  `get_profile` correctly shows `get_profile` depending on whatever
  `require_auth`'s wrapper calls (like a session check), not falsely
  attributed to `require_auth` itself
- Higher-order stdlib functions: `map(fn, items)`, `sorted(x, key=fn)`,
  `filter(fn, items)` — a function passed *by reference* to these is
  tracked as an implicit call

## Known limitations (genuinely unfixable by static analysis, not bugs)

- **Dynamic dispatch / `getattr()`-based routing**: `getattr(obj, name)()`
  can't be resolved statically when `name` is computed at runtime (from
  config, user input, etc.) — no static analysis tool can do this in
  general, including IDEs.
- **Cross-file changes related by theme, not by function calls**: e.g. "remove
  a dependency," touching 3 files for one conceptual reason with no direct
  call-graph edges between them. Blast radius is a call-graph tool; it
  cannot detect relatedness that isn't expressed as a function call.
- **User-defined higher-order functions**: only the common stdlib cases
  (`map`, `filter`, `sorted`/`max`/`min` with `key=`) are recognized.
  A custom function like `def apply_twice(fn, value): return fn(fn(value))`
  is not — this would need cross-function signature analysis to know which
  parameter is expected to be callable.

Run `grep -rn "function_name(" --include="*.py" .` to spot-check anything
important before fully trusting "no callers found."

## Architecture

```
diffcontext/
├── __init__.py          # Package entry, high-level API
├── models.py             # Data classes (Symbol, RepositoryIndex, etc.)
├── scanner.py             # File discovery with exclusion list
├── parser.py               # AST symbol extraction
├── resolver.py              # Import -> filesystem path resolution
├── symbols.py                 # Attribute / local-var type tracking
├── graph_builder.py             # Core: dependency graph construction
├── pipeline.py                    # Pipeline orchestrator
├── _warn_once.py                    # De-duplicated warnings (broken files, encoding, unknown symbols)
├── diff/
│   ├── git_diff.py                    # Git diff -> changed symbols
│   └── state_manager.py                # Snapshot-based change detection
├── impact/
│   ├── blast_radius.py                  # Reverse graph traversal
│   ├── scoring.py                         # Impact scoring
│   ├── traversal.py                         # Forward dependency expansion
│   └── visualizer.py                          # Terminal tree rendering
├── context/
│   ├── selector.py                              # Token-budget-aware selection
│   └── compiler.py                                # Structured output formatting
└── cli/
    └── __init__.py                                  # CLI: index, impact, diff, compile, blast
```

## How symbol IDs work

Every function gets a unique ID: `./relative/path.py:ClassName.method_name`

```
./src/auth.py:validate_jwt
./src/flask/app.py:Flask.route
```

**No parentheses, no arguments** — `validate_jwt`, never `validate_jwt(token)`.

## Testing

```bash
python3 -m pytest tests/ -v
```

25 tests, all self-contained (no external clone needed), covering both
correct resolution and the documented limitations above — including tests
that were written to fail loudly if a future change silently regresses
something that's currently working.

## Status

This is a personal project, built and iteratively debugged against a real
production codebase (not just synthetic test repos). Several real resolver
bugs were found and fixed through that process — see commit history and
`tests/test_graph_resolution.py` for specifics. It has not yet been
benchmarked for precision/recall against real git co-change history at
scale; treat blast-radius output as a strong starting point, not a
guarantee, and spot-check with `grep` on anything load-bearing.

## License

MIT