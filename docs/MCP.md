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

Parameters:
- `repo_path` (optional): repo path, defaults to `--repo`
- `changed_symbols` (optional): list of symbol IDs (e.g. `["./src/auth.py:validate_jwt"]`)
- `git_ref` (optional): git ref to detect changes from (e.g. `"HEAD~1"`)
- `max_tokens` (default 8000): token budget
- `meta` (default `"compact"`): header level — `"full"`, `"compact"`, or `"off"`

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
