# MCP Server

DiffContext ships an MCP (Model Context Protocol) server for Claude Code,
Cursor, Windsurf, and any MCP-compatible agent. It exposes four tools that
wrap the existing pipeline — no new retrieval logic, just the wire.

## Install

```bash
pip install "diffcontext[mcp]"
```

This adds the `mcp` SDK as an optional dependency. The core package stays
zero-dependency.

## Configure

Add to your MCP client config:

```json
{
  "mcpServers": {
    "diffcontext": {
      "command": "diffcontext-mcp",
      "args": ["--repo", "/path/to/your/project"]
    }
  }
}
```

The `--repo` flag sets a default repository path. Tools also accept
`repo_path` as a parameter, so an agent working across multiple repos can
pass it per-call.

## Tools

### `compile_context`

Compile LLM-ready context for a change. Give it changed symbol IDs or a
git ref, and it returns the callers, callees, and related functions packed
into a token budget with a disclosure header showing what was dropped.

Optionally pass `task_description` (the bug report or issue text) to bias
retrieval toward symbols relevant to the described problem — the one signal
the graph alone can't provide. When only `task_description` is given (no
`changed_symbols` or `git_ref`), changes are auto-detected from `HEAD`.

Parameters:
- `repo_path` (optional): repo path, defaults to `--repo`
- `changed_symbols` (optional): list of symbol IDs (e.g. `["./src/auth.py:validate_jwt"]`)
- `git_ref` (optional): git ref to detect changes from (e.g. `"HEAD~1"`)
- `task_description` (optional): bug report or issue text — biases retrieval
  toward symbols semantically related to the described problem
- `max_tokens` (default 8000): token budget
- `meta` (default `"full"`): header level — `"full"`, `"compact"`, or `"off"`.
  The pass@1 effect of meta level is UNMEASURED.

#### Example: plain-English bug report → cross-file context

Reproduced with diffcontext 0.5.4 against click at commit 2c8cd3a:

```bash
git clone --depth 1 https://github.com/pallets/click.git
diffcontext compile --repo click \
    --changed ./src/click/shell_completion.py:shell_complete \
    --query-text "shell completion for bash and zsh is broken" \
    --max-tokens 4000 --meta compact
```

Actual output (with `--query-text`):

```
=== DIFFCONTEXT META ===
Repo symbols total    : 524
Symbols IN context    : 10
Symbols DROPPED       : 514  ← you cannot see these
Changed symbols       : 1
Direct callers found  : 20
Context tokens (code) : 3,235

DROPPED SYMBOLS (514) — scored but cut by token budget:
  - ./src/click/shell_completion.py:ShellComplete.format_completion  (score: 64)
  - ./src/click/shell_completion.py:ZshComplete.format_completion  (score: 62)
  ... and 511 more

=== CHANGED SYMBOLS ===
FILE: ./src/click/shell_completion.py
FUNCTION: shell_complete (score: 115)
...

=== IMPACTED SYMBOLS ===
FILE: ./src/click/core.py
FUNCTION: Command._main_shell_completion (score: 82)
CALLEES: ./src/click/shell_completion.py:shell_complete
...

FILE: ./src/click/shell_completion.py
FUNCTION: BashComplete._check_version (score: 74)
...

FILE: ./src/click/shell_completion.py
FUNCTION: add_completion_class (score: 74)
...
```

Without `--query-text`, 9 symbols fit (not 10). `BashComplete._check_version`
ranks 17th among non-seed symbols (score 44.3) and is **dropped** — below
the 4000-token budget. With the bug report "shell completion for **bash**
and zsh is broken", it jumps to rank 3 (score 74.3): the `query_text` signal
BM25-matched "bash" against `BashComplete` in the code. The graph found it
(same-file sibling), but ranked it low; the problem description told the
ranker this is the specific part that's broken.

`Command._main_shell_completion` is in `core.py` — a different file from
the changed symbol, reached via a call edge. The query text moved it from
rank 4 to rank 2.

Then `diffcontext verify --from-history 20` grades retrieval on click's
own git history: 11/12 co-change cases pass (recall ≥ 50%), 1 fails. Real
grading, not a decorative number.

### `find_impact`

Find what breaks if you change a symbol. Returns the blast radius: direct
callers, direct callees, and transitive impact.

Parameters:
- `repo_path` (optional): repo path, defaults to `--repo`
- `symbol`: symbol ID (e.g. `./src/auth.py:validate_jwt`)

### `explain_selection`

Explain why symbols were included or dropped from context. Returns JSON
with `included_symbols` (id, role, score, tokens) and `dropped_symbols`
(id, score), so an agent can inspect or filter the selection.

Parameters:
- `repo_path` (optional): repo path, defaults to `--repo`
- `symbol`: the changed symbol ID to build context for
- `max_tokens` (default 8000): token budget

### `verify_retrieval`

Mine git history and grade retrieval quality on your repo. Generates test
cases from co-change history, runs retrieval against them, and reports
hit/recall. **Prints NULL RESULT when the tool doesn't fit your repo** —
finding that out is the feature.

Parameters:
- `repo_path` (optional): repo path, defaults to `--repo`
- `n` (default 20): max test cases to generate from git history
