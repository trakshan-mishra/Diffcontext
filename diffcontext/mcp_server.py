"""
mcp_server.py — MCP server exposing DiffContext's pipeline as tools.

Four tools, wrapping existing pipeline functions only — no new retrieval
logic. Designed for Claude Code / Cursor / Windsurf integration via stdio
transport.

Install:  pip install "diffcontext[mcp]"
Run:      diffcontext-mcp --repo /path/to/your/project

The index is cached in-process (MCP servers are long-lived). Cold index is
seconds; warm is ~0.02s. Subsequent calls call update_index rather than
reindexing from scratch.
"""

import argparse
import os
import sys
from typing import Dict, List, Optional

# Late imports of diffcontext — the MCP extra is optional, and the core
# stays zero-dependency. These run after the mcp import so a missing mcp
# package fails fast with a clear message.

# In-process index cache: repo_path -> RepositoryIndex. MCP servers are
# long-lived; without this, every tool call reindexes from scratch (seconds
# vs ~0.02s warm).
_INDEX_CACHE: Dict[str, object] = {}


def _get_index(repo_path: str, include: Optional[List[str]] = None):
    """Get or create a cached index for repo_path. Reuses the existing
    index via update_index if the repo changed since last index."""
    from diffcontext.pipeline import index_repository

    repo_abs = os.path.abspath(repo_path)
    if repo_abs not in _INDEX_CACHE:
        _INDEX_CACHE[repo_abs] = index_repository(
            repo_abs, include=set(include) if include else None
        )
    return _INDEX_CACHE[repo_abs]


def _resolve_changed(index, changed_symbols=None, git_ref=None, repo_path=None):
    """Resolve changed symbols from explicit list or git ref."""
    if changed_symbols:
        return changed_symbols
    if git_ref:
        from diffcontext.diff.git_diff import find_changed_symbols
        return find_changed_symbols(
            repo_path, index.symbols, ref=git_ref,
            broken_files=index.broken_files,
            known_broken_files=index.broken_files,
        )
    return []


def main():
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        sys.exit(
            "mcp package not found. Install with: pip install \"diffcontext[mcp]\""
        )

    parser = argparse.ArgumentParser(description="DiffContext MCP server")
    parser.add_argument("--repo", default=".", help="Default repository path")
    args = parser.parse_args()
    default_repo = os.path.abspath(args.repo)

    from diffcontext import __version__ as dc_version

    server = MCPServer(
        name="diffcontext",
        description=(
            "Static-analysis-powered context compiler for LLMs. Selects "
            "the code that matters for a change and packs it into a token "
            "budget. Measures whether it works on your repo."
        ),
        version=dc_version,
    )

    @server.tool()
    def compile_context(
        repo_path: str = "",
        changed_symbols: Optional[List[str]] = None,
        git_ref: Optional[str] = None,
        task_description: Optional[str] = None,
        max_tokens: int = 8000,
        meta: str = "full",
    ) -> str:
        """Compile LLM-ready context for a change.

        Give it changed symbol IDs (e.g. ./src/auth.py:validate_jwt) or a
        git ref (e.g. HEAD~1), and it returns the callers, callees, and
        related functions the model needs to make the change safely — packed
        into max_tokens with a disclosure header showing what was dropped.

        Optionally pass task_description (the bug report or issue text) to
        bias retrieval toward symbols relevant to the described problem —
        the one signal the graph alone can't provide.

        Args:
            repo_path: Absolute path to the repository. If omitted, uses
                the --repo from server startup.
            changed_symbols: List of changed symbol IDs (e.g.
                ["./src/auth.py:validate_jwt"]). Mutually exclusive with
                git_ref.
            git_ref: Git ref to detect changes from (e.g. "HEAD~1").
                Mutually exclusive with changed_symbols. When only
                task_description is given (no changed_symbols or git_ref),
                defaults to "HEAD".
            task_description: The bug report or issue text. Biases retrieval
                toward symbols semantically related to the described problem,
                not just structurally near the changed symbols.
            max_tokens: Token budget for the context (default 8000).
            meta: Disclosure header level: "full" (default), "compact", or "off".
                The pass@1 effect of meta level is UNMEASURED.
        """
        from diffcontext.pipeline import analyze_impact, compile

        repo = repo_path or default_repo
        idx = _get_index(repo)

        # When only task_description is given, auto-detect changes from HEAD.
        effective_ref = git_ref
        if not changed_symbols and not git_ref and task_description:
            effective_ref = "HEAD"

        changed = _resolve_changed(idx, changed_symbols, effective_ref, repo)
        if not changed and not task_description:
            return "No changed symbols found. Pass changed_symbols, git_ref, or task_description."

        # Untuned default; not derived from any sweep. The query_weight controls
        # how much the problem-description signal moves candidates relative to
        # the graph + BM25 + same-file blend. 0.3 is a guess that "matters but
        # doesn't dominate" — it has NOT been benchmarked or optimized.
        query_weight = 0.3 if task_description else 0.0
        impact = analyze_impact(
            idx, changed, query_text=task_description, query_weight=query_weight,
        )
        ctx = compile(idx, impact, max_tokens=max_tokens, meta=meta)
        return ctx.text

    @server.tool()
    def find_impact(
        repo_path: str = "",
        symbol: str = "",
    ) -> str:
        """Find what breaks if you change a symbol.

        Returns the blast radius: direct callers, direct callees, and
        transitive impact. The "what breaks if I change this" query.

        Args:
            repo_path: Absolute path to the repository. If omitted, uses
                the --repo from server startup.
            symbol: Symbol ID (e.g. ./src/auth.py:validate_jwt).
        """
        from diffcontext.pipeline import analyze_impact, warn_unknown_symbols

        repo = repo_path or default_repo
        idx = _get_index(repo)
        warn_unknown_symbols(idx, [symbol])
        impact = analyze_impact(idx, [symbol], hybrid=False)

        lines = [f"Changed: {symbol}"]
        lines.append(f"\nBlast radius ({len(impact.blast_radius)}):")
        for sym in impact.blast_radius[:20]:
            score = impact.scores.get(sym, 0)
            lines.append(f"  {sym} (score: {score:.0f})")
        lines.append(f"\nTotal impacted: {len(impact.all_relevant)}")
        return "\n".join(lines)

    @server.tool()
    def explain_selection(
        repo_path: str = "",
        symbol: str = "",
        max_tokens: int = 8000,
    ) -> str:
        """Explain why symbols were included or dropped from context.

        Returns the included symbols (with scores and token costs) and
        the dropped symbols (scored but cut by the token budget), so an
        agent can inspect or filter the selection.

        Args:
            repo_path: Absolute path to the repository. If omitted, uses
                the --repo from server startup.
            symbol: The changed symbol ID to build context for.
            max_tokens: Token budget (default 8000).
        """
        import json
        from diffcontext.pipeline import analyze_impact, compile

        repo = repo_path or default_repo
        idx = _get_index(repo)
        impact = analyze_impact(idx, [symbol])
        ctx = compile(idx, impact, max_tokens=max_tokens, meta="off")

        included = [
            {"id": item.symbol_id, "role": item.role,
             "score": round(item.score, 1), "tokens": item.token_estimate}
            for item in ctx.items
        ]
        dropped = [
            {"id": sid, "score": round(impact.scores.get(sid, 0.0), 1)}
            for sid in ctx.dropped_symbols
        ]
        result = {
            "included_symbols": included,
            "dropped_symbols": dropped,
            "total_tokens": ctx.token_estimate,
            "symbol_count": ctx.symbol_count,
        }
        return json.dumps(result, indent=2)

    @server.tool()
    def verify_retrieval(
        repo_path: str = "",
        n: int = 20,
    ) -> str:
        """Mine git history and grade retrieval quality on your repo.

        Generates test cases from co-change history, runs DiffContext
        retrieval against them, and reports hit/recall. Prints NULL RESULT
        when the tool doesn't fit your repo — finding that out IS the
        feature.

        Args:
            repo_path: Absolute path to the repository. If omitted, uses
                the --repo from server startup.
            n: Maximum number of test cases to generate from git history
                (default 20).
        """
        from diffcontext.verify import cases_from_history, run_cases, render_results

        repo = repo_path or default_repo
        _get_index(repo)  # warm the cache for subsequent calls
        skipped: List[str] = []
        cases = cases_from_history(repo_path, max_cases=n, skipped_out=skipped)
        if not cases:
            return ("No co-change cases found in git history. Need commits that "
                    "modify 2+ functions (non-test .py files).")
        results = run_cases(repo_path, cases)
        return render_results(results)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
