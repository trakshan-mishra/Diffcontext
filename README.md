# DiffContext

**Static-analysis-powered repository context compiler for LLMs.**

> Git diff + AST parsing + dependency graph + blast radius + impact scoring → optimized context package

Instead of dumping an entire codebase (or doing keyword/vector search), DiffContext:

1. **Parses** Python files via AST → extracts every function, method, class
2. **Builds a dependency graph** → calls, imports, inheritance, attribute ownership
3. **Detects changes** → via git diff or snapshot comparison
4. **Computes blast radius** → everything transitively affected by the change
5. **Scores impact** → prioritizes symbols by structural importance
6. **Selects relevant code** → respects token budget
7. **Compiles context** → structured output ready for an LLM

---

## Quick Start (Complete Noob Guide)

### Step 1: Install

```bash
# Clone this repo
git clone https://github.com/trakshan-mishra/Diffcontext.git
cd Diffcontext

# Install globally (the "diffcontext" command becomes available everywhere)
pip install -e ".[bench]"

# Verify it works
diffcontext --help
```

### Step 2: Index a repository

Point DiffContext at any Python project:

```bash
diffcontext index /path/to/any/python/project
```

Example output:
```
Symbols : 354
Edges   : 160
Time    : 548ms
Files   : 20
```

### Step 3: Analyze impact of a change

If you know which function changed:

```bash
diffcontext impact ./src/auth.py:validate_jwt --repo /path/to/repo
```

Output shows the **blast radius** — everything affected by that change.

### Step 4: Auto-detect changes from git

```bash
diffcontext diff HEAD~1 --repo /path/to/repo
```

Finds which functions were modified in the last commit.

### Step 5: Build LLM context

```bash
# From specific changed functions:
diffcontext compile --changed ./src/auth.py:validate_jwt --repo /path/to/repo

# From git diff:
diffcontext compile --ref HEAD~1 --repo /path/to/repo

# With token budget:
diffcontext compile --changed ./src/auth.py:validate_jwt --repo /path/to/repo --max-tokens 8000

# JSON output:
diffcontext compile --changed ./src/auth.py:validate_jwt --repo /path/to/repo --json
```

### Step 6: Use in Python code

```python
from diffcontext.pipeline import index_repository, analyze_impact, compile

# 1. Index the repo (do once, reuse)
idx = index_repository("/path/to/repo")
print(f"{len(idx.symbols)} symbols, {idx.total_edges} edges")

# 2. Analyze impact of a change
impact = analyze_impact(idx, ["./src/auth.py:validate_jwt"])
print(f"Blast radius: {len(impact.blast_radius)} affected symbols")

# 3. Compile context for LLM
ctx = compile(idx, impact, max_tokens=10000)
print(ctx.text)            # The context to send to the LLM
print(f"Tokens: {ctx.token_estimate:,} / {ctx.total_repo_tokens:,}")
print(f"Reduction: {ctx.reduction_pct:.1f}%")
```

---

## Benchmarking (Research-Grade)

### Step 1: Clone real repos with git history

```bash
python benchmark_runner.py --clone
```

This clones Flask, Click, httpx, Pydantic with 100 commits of history.

### Step 2: Run co-change benchmark (honest, non-circular)

```bash
python benchmark_runner.py --cochange
```

**Ground truth**: Functions that human developers changed together in the same commit.
This is external evidence — completely independent of our dependency graph.

### Step 3: Compare against baselines

```bash
python benchmark_runner.py --compare
```

Compares DiffContext vs BM25 vs file co-location vs random.

### Step 4: Run everything

```bash
python benchmark_runner.py --full
```

### Results

DiffContext wins on F1 in **3/4 real repos** against BM25 (the standard IR baseline):

| Repo | DiffContext F1 | BM25 F1 | Winner |
|---|---|---|---|
| click | **0.136** | 0.079 | DiffContext (+72%) |
| flask | **0.133** | 0.119 | DiffContext (+12%) |
| httpx | 0.219 | 0.221 | Tie (File-CoLoc wins at 0.260) |
| pydantic | **0.130** | 0.078 | DiffContext (+67%) |

**DiffContext has 2-3x higher precision** — it sends less irrelevant code to the LLM.

---

## Architecture

```
diffcontext/
├── __init__.py          # Package entry
├── models.py            # Data classes (Symbol, RepositoryIndex, etc.)
├── scanner.py           # File discovery with exclusion list
├── parser.py            # AST symbol extraction
├── resolver.py          # Import → filesystem path resolution
├── symbols.py           # Attribute ownership (self.x type tracking)
├── graph_builder.py     # Core: dependency graph construction
├── pipeline.py          # Pipeline orchestrator
├── diff/
│   ├── git_diff.py      # Git diff → changed symbols
│   └── state_manager.py # Snapshot-based change detection
├── impact/
│   ├── blast_radius.py  # Reverse graph traversal
│   ├── scoring.py       # Impact scoring (blast_radius*3 + indegree*2 + outdegree)
│   └── traversal.py     # Forward dependency expansion
├── context/
│   ├── selector.py      # Token-budget-aware selection
│   └── compiler.py      # Structured output formatting
└── cli/
    └── __init__.py      # CLI commands: index, impact, diff, compile
```

---

## How Symbol IDs Work

Every function gets a unique ID: `./relative/path.py:ClassName.method_name`

Examples:
```
./src/flask/app.py:Flask.route
./src/flask/app.py:Flask.wsgi_app
./src/flask/helpers.py:url_for
./auth.py:validate_jwt
```

---

## License

MIT
