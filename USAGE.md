# DiffContext — Day-to-Day Usage

This captures the actual workflow that works, based on real use against a
1000+ symbol production repo (not just toy examples).

## Setup (once per shell session, or add to ~/.bashrc / ~/.zshrc)

```bash
alias dcb='diffcontext blast --changed'
alias dcc='diffcontext compile --changed'
alias dci='diffcontext index'
```

To make these permanent, append the three lines above to `~/.bashrc` (or
`~/.zshrc` if you use zsh), then `source ~/.bashrc`.

## The core workflow

### 1. While actively editing — don't rely on git diff detection

Git-diff-based commands (`diffcontext diff`, `diffcontext blast` with no
`--changed`) only see **tracked** changes that are committed or staged.
An edit to an **untracked** (brand new) file is invisible to them — not a
bug, that's just what `git diff` means.

For active editing, skip git entirely and name the symbol directly:

```bash
dcb ./path/to/file.py:function_name
```

This works immediately, no commit, no `git add`, no staging.

### 2. Symbol IDs — exact format

```
./relative/path.py:function_name
./relative/path.py:ClassName.method_name
```

Rules:
- Path is relative to the repo root you indexed, always starts with `./`
- **No parentheses, no arguments, no type hints** — `update_run`, never
  `update_run(run_id: int, **kwargs)`. Bash will choke on unquoted `()`
  with a `syntax error near unexpected token` — that's bash, not
  diffcontext, complaining.
- Find real names fast:
  ```bash
  grep -n "^def \|^    def " path/to/file.py
  ```

### 3. Before trusting "no callers found" — spot-check with grep

This caught 3 real bugs during testing. Make it a habit, not a one-off:

```bash
grep -rn "function_name(" --include="*.py" .
```

If grep finds callers diffcontext's blast radius missed, that's a real
gap worth knowing about (and worth reporting) — don't assume the blast
radius is complete just because it ran without error.

### 4. Getting LLM-ready context

```bash
dcc ./path/to/file.py:function_name --max-tokens 4000
```

Paste the output into Claude/ChatGPT **with a specific question**, not
just the raw context:

- Bad: "review this"
- Good: "I'm about to add a new field to `update_run` — given these 5
  callers, what do I need to check?"
- Good: "Is the dynamic SQL construction in `update_run` safe given how
  `kwargs` is validated against `_UPDATABLE_RUN_COLUMNS`?"

### 5. Tuning context size

- `--depth N` (default 2-3): how many hops of callers/callees to pull in.
  Use `--depth 1` for a tight, single-function check. Use `--depth 4+`
  for "how does this fit into the bigger picture."
- `--max-tokens N`: hard cap. Lower it to force tighter selection (only
  the highest-scored symbols survive); raise it if you have a
  large-context model and want more surrounding code.

### 6. Checking what changed (only works for committed/staged files)

```bash
diffcontext diff                    # working tree vs HEAD~1, tracked files only
diffcontext diff --committed-only   # two commits only, ignores uncommitted edits
```

If a file shows as broken (`Skipping X due to SyntaxError`), diffcontext
will still report a best-effort diff using the prior committed version —
look for the `⚠ N file(s) failed to parse` block in the output.

## Known limitations (don't trust blast radius blindly here)

- **Decorators / closures**: a decorated function's real dependency
  (via its wrapper) won't show up. If you're touching a decorator or a
  function that's heavily decorated, grep-check manually.
- **Functions passed by reference**: `map(fn, items)`, `sorted(key=fn)`
  — `fn` won't show as "called" since there's no direct call site.
- **Dynamic dispatch / `getattr()`-based routing**: common in CLI
  argument dispatch and plugin systems — invisible to static analysis.
- **Cross-file changes related by theme, not by function calls**: e.g.
  "remove a dependency" touching 3 unrelated-by-call-graph files for one
  conceptual reason. Blast radius won't connect these.

When in doubt: grep first, trust second.
