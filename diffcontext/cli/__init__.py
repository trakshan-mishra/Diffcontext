"""
cli/main.py — DiffContext command-line interface.

Usage:
    diffcontext index .
    diffcontext impact auth.py:validate_jwt
    diffcontext diff HEAD~1
    diffcontext compile --changed ./api.py:get_user
    diffcontext blast --ref HEAD~1          # visual blast radius
    diffcontext blast --ref HEAD~1 --verify # with proof chains
    diffcontext verify --ref HEAD~1                    # sufficiency report
    diffcontext verify --cases cases.json              # user test cases
    diffcontext verify --from-history 30 --calibrate   # score vs measured recall
"""

import argparse
import json
import logging
import os
import sys
import time

from ..pipeline import index_repository, analyze_impact, compile, warn_unknown_symbols
from ..diff.git_diff import find_changed_symbols
from ..impact.visualizer import render_blast_radius, render_verification


def main():
    # Make sure warnings from anywhere in the pipeline (broken files,
    # invalid encoding, unknown --changed symbols) are actually visible.
    # Without this, they depend on logging's lastResort fallback, which is
    # unreliable -- some warnings showed up by accident, others silently
    # didn't, depending on subtle propagation/level details.
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)

    parser = argparse.ArgumentParser(
        prog="diffcontext",
        description="Static-analysis-powered repository context compiler for LLMs",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # --- index ---
    p_index = sub.add_parser("index", help="Index a repository")
    p_index.add_argument("repo", default=".", nargs="?", help="Path to repository")

    # --- impact ---
    p_impact = sub.add_parser("impact", help="Analyze impact of a symbol change")
    p_impact.add_argument("symbols", nargs="+", help="Changed symbol IDs (e.g. ./auth.py:validate_jwt)")
    p_impact.add_argument("--repo", default=".", help="Repository path")
    p_impact.add_argument("--depth", type=int, default=2, help="Max dependency depth")
    p_impact.add_argument("--tree", action="store_true", help="Show visual blast radius tree")
    p_impact.add_argument("--verify", action="store_true", help="Show proof chains for each edge")

    # --- diff ---
    p_diff = sub.add_parser("diff", help="Find changed symbols from git diff")
    p_diff.add_argument("ref", default="HEAD~1", nargs="?", help="Git ref to compare against")
    p_diff.add_argument("--repo", default=".", help="Repository path")
    p_diff.add_argument(
        "--committed-only", action="store_true",
        help="Compare two commits only (ref vs HEAD); ignores uncommitted working-tree changes",
    )

    # --- compile ---
    p_compile = sub.add_parser("compile", help="Build LLM context for changes")
    p_compile.add_argument("--changed", nargs="+", help="Changed symbol IDs")
    p_compile.add_argument("--ref", default=None, help="Git ref (auto-detect changes)")
    p_compile.add_argument("--repo", default=".", help="Repository path")
    p_compile.add_argument("--depth", type=int, default=2, help="Max dependency depth")
    p_compile.add_argument("--max-tokens", type=int, default=10000, help="Token budget")
    p_compile.add_argument(
        "--top-k", type=int, default=20,
        help="Max context symbols per changed symbol (benchmarked sweet spot: 20; 0 = unlimited)",
    )
    p_compile.add_argument(
        "--graph-only", action="store_true",
        help="Disable the hybrid (graph+BM25+same-file) blend and rank by call graph alone",
    )
    p_compile.add_argument(
        "--with-history", action="store_true",
        help="Blend git co-change history as a fourth signal (mines git log once; "
             "reaches related files with no call or lexical connection)",
    )
    p_compile.add_argument("--notes", type=str, default=None, help="Developer notes to prepend to the context output")
    p_compile.add_argument("--json", action="store_true", help="Output as JSON")

    # --- blast (NEW: visual blast radius) ---
    p_blast = sub.add_parser("blast", help="Visual blast radius analysis")
    p_blast.add_argument("--changed", nargs="+", help="Changed symbol IDs (manual)")
    p_blast.add_argument("--ref", default=None, help="Git ref (auto-detect changes)")
    p_blast.add_argument("--repo", default=".", help="Repository path")
    p_blast.add_argument("--depth", type=int, default=3, help="Max traversal depth for tree")
    p_blast.add_argument("--verify", action="store_true", help="Show proof chains for each edge")
    p_blast.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    p_blast.add_argument(
        "--committed-only", action="store_true",
        help="Compare two commits only (ref vs HEAD); ignores uncommitted working-tree changes",
    )

    # --- verify (sufficiency + test cases + calibration) ---
    p_verify = sub.add_parser(
        "verify",
        help="Score context sufficiency; run user test cases; calibrate the score",
    )
    p_verify.add_argument("--changed", nargs="+", help="Changed symbol IDs")
    p_verify.add_argument("--ref", default=None, help="Git ref (auto-detect changes)")
    p_verify.add_argument("--repo", default=".", help="Repository path")
    p_verify.add_argument("--depth", type=int, default=2, help="Max dependency depth")
    p_verify.add_argument("--max-tokens", type=int, default=10000, help="Token budget (0 = unlimited)")
    p_verify.add_argument(
        "--top-k", type=int, default=20,
        help="Max context symbols per changed symbol (0 = unlimited)",
    )
    p_verify.add_argument(
        "--cases", default=None, metavar="FILE",
        help="Run test cases from a JSON/YAML file (see docs/VERIFY.md for format)",
    )
    p_verify.add_argument(
        "--from-history", type=int, default=None, metavar="N",
        help="Auto-generate up to N test cases from git co-change history",
    )
    p_verify.add_argument(
        "--out", default=None, metavar="FILE",
        help="With --from-history: write generated cases to FILE instead of running them",
    )
    p_verify.add_argument(
        "--calibrate", action="store_true",
        help="With --cases/--from-history: report how the structural score tracks measured recall",
    )
    p_verify.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "index":
        _cmd_index(args)
    elif args.command == "impact":
        _cmd_impact(args)
    elif args.command == "diff":
        _cmd_diff(args)
    elif args.command == "compile":
        _cmd_compile(args)
    elif args.command == "blast":
        _cmd_blast(args)
    elif args.command == "verify":
        _cmd_verify(args)


def _cmd_index(args):
    """Index repository: show stats."""
    t0 = time.perf_counter()
    idx = index_repository(args.repo)
    elapsed = (time.perf_counter() - t0) * 1000

    print(f"Symbols : {len(idx.symbols)}")
    print(f"Edges   : {idx.total_edges}")
    print(f"Time    : {elapsed:.0f}ms")

    # Show top-level breakdown
    files = set()
    for sym in idx.symbols.values():
        files.add(sym.file)
    print(f"Files   : {len(files)}")

    if idx.broken_files:
        print(f"Broken  : {len(idx.broken_files)} file(s) failed to parse (see warnings above)")


def _cmd_impact(args):
    """Analyze impact of specific symbol changes."""
    idx = index_repository(args.repo)
    impact = analyze_impact(idx, args.symbols, max_depth=args.depth)

    if getattr(args, 'tree', False) or getattr(args, 'verify', False):
        # Visual tree mode
        output = render_blast_radius(
            idx.graph, args.symbols, idx.symbols,
            max_depth=args.depth,
            show_proof=getattr(args, 'verify', False),
            repo_path=os.path.abspath(args.repo),
        )
        print(output)

        if getattr(args, 'verify', False):
            verification = render_verification(
                idx.graph, args.symbols, idx.symbols,
            )
            print(verification)
    else:
        # Original text mode
        print(f"\nChanged: {impact.changed}")
        print(f"\nBlast radius ({len(impact.blast_radius)}):")
        for sym in impact.blast_radius[:20]:
            score = impact.scores.get(sym, 0)
            print(f"  {sym} (score: {score:.0f})")

        print(f"\nTotal impacted: {len(impact.all_relevant)}")


def _print_broken_files(idx, broken_patches):
    """Shared helper: print patch text for any files that failed to parse."""
    if not idx.broken_files:
        return

    print(f"\n⚠ {len(idx.broken_files)} file(s) failed to parse and could not be fully analyzed:")
    for f in idx.broken_files:
        print(f"\n--- {f} ---")
        patch = broken_patches.get(f)
        if patch:
            print(patch.rstrip("\n"))
        else:
            print("  (no patch text available -- file may be new/untracked)")


def _cmd_diff(args):
    """Find changed symbols from git diff."""
    idx = index_repository(args.repo)

    against = "HEAD" if args.committed_only else None
    broken_patches = {}
    changed = find_changed_symbols(
        args.repo, idx.symbols, ref=args.ref, against=against,
        broken_files=idx.broken_files,
        broken_file_patches=broken_patches,
        known_broken_files=idx.broken_files,
    )

    if not changed:
        print("No changed symbols found.")
        _print_broken_files(idx, broken_patches)
        return

    print(f"Changed symbols ({len(changed)}):")
    for sym_id in changed:
        print(f"  {sym_id}")

    _print_broken_files(idx, broken_patches)


def _cmd_compile(args):
    """Build full context package."""
    idx = index_repository(args.repo)

    # Determine changed symbols
    if args.changed:
        changed = args.changed
    elif args.ref:
        changed = find_changed_symbols(
            args.repo, idx.symbols, ref=args.ref,
            broken_files=idx.broken_files,
            known_broken_files=idx.broken_files,
        )
    else:
        print("Error: provide --changed or --ref", file=sys.stderr)
        sys.exit(1)

    if not changed:
        print("No changes detected.")
        return

    history = None
    if getattr(args, "with_history", False):
        from ..history import CoChangeIndex
        history = CoChangeIndex(args.repo)

    impact = analyze_impact(
        idx, changed, max_depth=args.depth, hybrid=not args.graph_only,
        history=history,
    )
    max_tokens = args.max_tokens if args.max_tokens > 0 else None
    top_k = args.top_k * len(changed) if args.top_k > 0 else None
    ctx = compile(idx, impact, max_tokens=max_tokens, notes=args.notes, top_k=top_k)

    if args.json:
        result = {
            "symbol_count": ctx.symbol_count,
            "token_estimate": ctx.token_estimate,
            "total_repo_tokens": ctx.total_repo_tokens,
            "reduction_pct": round(ctx.reduction_pct, 2),
            "context": ctx.text,
        }
        print(json.dumps(result, indent=2))
    else:
        print(ctx.text)
        print(f"\n--- Stats ---")
        print(f"Symbols  : {ctx.symbol_count}")
        print(f"Tokens   : {ctx.token_estimate:,} / {ctx.total_repo_tokens:,}")
        print(f"Reduction: {ctx.reduction_pct:.1f}%")


def _cmd_blast(args):
    """Visual blast radius analysis."""
    t0 = time.perf_counter()
    idx = index_repository(args.repo)
    index_ms = (time.perf_counter() - t0) * 1000

    against = "HEAD" if getattr(args, "committed_only", False) else None
    broken_patches = {}

    # Determine changed symbols
    if args.changed:
        changed = args.changed
    elif args.ref:
        changed = find_changed_symbols(
            args.repo, idx.symbols, ref=args.ref, against=against,
            broken_files=idx.broken_files, broken_file_patches=broken_patches,
            known_broken_files=idx.broken_files,
        )
    else:
        # Default: compare against HEAD~1
        changed = find_changed_symbols(
            args.repo, idx.symbols, ref="HEAD~1", against=against,
            broken_files=idx.broken_files, broken_file_patches=broken_patches,
            known_broken_files=idx.broken_files,
        )

    if not changed:
        print("No changed symbols detected.")
        print("  Tip: make a Python change and commit it, or use --changed <symbol_id>")
        print(f"  Available symbols: {len(idx.symbols)} (use 'diffcontext index' to see stats)")
        _print_broken_files(idx, broken_patches)
        return

    # blast renders directly from idx.graph and never calls analyze_impact,
    # so it needs its own unknown-symbol check (typo'd --changed, renamed/
    # deleted symbol) -- otherwise a typo silently renders as "0 impact"
    # indistinguishable from a real, genuinely-isolated symbol.
    warn_unknown_symbols(idx, changed)

    # Strip ANSI if --no-color
    if args.no_color:
        from ..impact import visualizer
        visualizer._C.RED = ""
        visualizer._C.YELLOW = ""
        visualizer._C.GREEN = ""
        visualizer._C.CYAN = ""
        visualizer._C.MAGENTA = ""
        visualizer._C.BLUE = ""
        visualizer._C.DIM = ""
        visualizer._C.BOLD = ""
        visualizer._C.RESET = ""
        visualizer._C.WHITE = ""

    # Render visual blast radius
    output = render_blast_radius(
        idx.graph, changed, idx.symbols,
        max_depth=args.depth,
        show_proof=args.verify,
        repo_path=os.path.abspath(args.repo),
    )
    print(output)

    # If --verify, also show detailed proof chains
    if args.verify:
        verification = render_verification(
            idx.graph, changed, idx.symbols,
        )
        print(verification)

    _print_broken_files(idx, broken_patches)

    # Timing footer
    total_ms = (time.perf_counter() - t0) * 1000
    print(f"  Indexed {len(idx.symbols)} symbols in {index_ms:.0f}ms")
    print(f"  Total analysis time: {total_ms:.0f}ms")
    print()


def _cmd_verify(args):
    """Sufficiency report, user test cases, and calibration."""
    from ..verify import (
        analyze_sufficiency, load_cases, save_cases, run_cases,
        cases_from_history, calibrate, render_results, render_calibration,
        CaseFormatError,
    )

    # ── Mode 1/2: test cases (from file or from git history) ─────────────
    if args.cases or args.from_history is not None:
        if args.cases:
            try:
                cases = load_cases(args.cases)
            except (CaseFormatError, OSError) as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            cases = cases_from_history(args.repo, max_cases=args.from_history)
            if not cases:
                print(
                    "No co-change cases found in git history. Need commits that "
                    "modify 2+ functions (non-test .py files).",
                    file=sys.stderr,
                )
                sys.exit(1)
            if args.out:
                save_cases(cases, args.out)
                print(f"Wrote {len(cases)} case(s) to {args.out}")
                print("Edit them (they're noisy — commits touch unrelated code too),")
                print(f"then run: diffcontext verify --cases {args.out} --calibrate")
                return

        results = run_cases(args.repo, cases)

        if args.json:
            payload = {"results": [r.to_dict() for r in results]}
            if args.calibrate:
                payload["calibration"] = calibrate(results).to_dict()
            print(json.dumps(payload, indent=2))
        else:
            print(render_results(results))
            if args.calibrate:
                print()
                print(render_calibration(calibrate(results)))

        sys.exit(0 if all(r.passed for r in results) else 1)

    # ── Mode 3: single sufficiency report for a change ────────────────────
    idx = index_repository(args.repo)

    if args.changed:
        changed = args.changed
    elif args.ref:
        changed = find_changed_symbols(
            args.repo, idx.symbols, ref=args.ref,
            broken_files=idx.broken_files,
            known_broken_files=idx.broken_files,
        )
    else:
        print("Error: provide --changed, --ref, --cases, or --from-history", file=sys.stderr)
        sys.exit(1)

    if not changed:
        print("No changes detected.")
        return

    warn_unknown_symbols(idx, changed)
    impact = analyze_impact(idx, changed, max_depth=args.depth)
    max_tokens = args.max_tokens if args.max_tokens > 0 else None
    top_k = args.top_k * len(changed) if args.top_k > 0 else None
    ctx = compile(idx, impact, max_tokens=max_tokens, top_k=top_k)

    report = analyze_sufficiency(idx, impact, ctx)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render())

    # Exit code mirrors the verdict so CI can gate on it.
    sys.exit(0 if report.verdict == "SUFFICIENT" else 1)


if __name__ == "__main__":
    main()


def cli_main():
    """
    Entry point for the `diffcontext` console script.

    Wraps main() to handle BrokenPipeError gracefully -- this happens
    whenever stdout is piped into something that closes early (a missing
    command, `head`, a reader that exits before reading everything). Without
    this, piping `diffcontext compile | some-missing-tool` prints a full
    Python traceback even though nothing is actually wrong.
    """
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Redirect remaining stdout to devnull so the interpreter's own
        # shutdown-time flush doesn't also raise BrokenPipeError.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(1)