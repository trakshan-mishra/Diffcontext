"""
eval_real_repos.py
==================
REAL precision/recall evaluation against actual repos.
NOT hardcoded toy functions — ground truth derived from reading actual source.

Usage:
    python eval_real_repos.py --repo benchmarks/requests/requests/src/requests
    python eval_real_repos.py --repo benchmarks/fastapi/fastapi/fastapi
    python eval_real_repos.py --all

Ground truth policy:
  For each "changed" function we manually trace:
    - DIRECT callees (functions it calls)
    - BLAST RADIUS (functions that call it, i.e. who breaks if we change it)
  We only require the system to find DIRECT 1-hop context (not transitive closure).
  This is the most meaningful signal: if you changed X, do we surface
  what X calls + what calls X?  That's what an LLM needs to do the fix.

Relevance = function appears in 1-hop neighborhood of changed function.
"""

import sys
import os
import argparse
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from multi_file_dependency_graph import build_repository_graph
from blast_radius import get_blast_radius
from dependency_expander import expand_dependencies
from benchmarks.evaluator import run_diffcontext


# ------------------------------------------------------------------ #
#  GROUND TRUTH: manually verified 1-hop neighborhoods               #
#  Format: { "description": str,                                      #
#             "changed": str (fn id relative to repo root),           #
#             "must_include": [fn_id, ...]  (any subset suffices)     #
#             "must_not_include": [fn_id, ...] (false positives)      #
#           }                                                          #
# ------------------------------------------------------------------ #

REQUESTS_CASES = [
    {
        "desc": "Session.get → must surface Session.request (direct callee)",
        "changed": "./sessions.py:Session.get",
        "must_include": ["./sessions.py:Session.request"],
        "must_not_include": [],
    },
    {
        "desc": "Session.post → must surface Session.request",
        "changed": "./sessions.py:Session.post",
        "must_include": ["./sessions.py:Session.request"],
        "must_not_include": [],
    },
    {
        "desc": "Session.send → blast radius must include Session.get, Session.post (they call send)",
        "changed": "./sessions.py:Session.send",
        "must_include": [
            "./sessions.py:Session.get",
            "./sessions.py:Session.post",
            "./sessions.py:Session.request",
        ],
        "must_not_include": [],
    },
    {
        "desc": "HTTPDigestAuth.__call__ → callees: init_per_thread_state, build_digest_header",
        "changed": "./auth.py:HTTPDigestAuth.__call__",
        "must_include": [
            "./auth.py:HTTPDigestAuth.init_per_thread_state",
            "./auth.py:HTTPDigestAuth.build_digest_header",
        ],
        "must_not_include": [],
    },
    {
        "desc": "merge_setting → blast radius must include Session.prepare_request",
        "changed": "./sessions.py:merge_setting",
        "must_include": ["./sessions.py:Session.prepare_request"],
        "must_not_include": [],
    },
    {
        "desc": "get_encoding_from_headers → direct callee: _parse_content_type_header",
        "changed": "./utils.py:get_encoding_from_headers",
        "must_include": ["./utils.py:_parse_content_type_header"],
        "must_not_include": [],
    },
    {
        "desc": "should_bypass_proxies → callee: is_ipv4_address, is_valid_cidr, address_in_network",
        "changed": "./utils.py:should_bypass_proxies",
        "must_include": [
            "./utils.py:is_ipv4_address",
            "./utils.py:is_valid_cidr",
            "./utils.py:address_in_network",
        ],
        "must_not_include": [],
    },
    {
        "desc": "resolve_proxies → callee: should_bypass_proxies, get_environ_proxies",
        "changed": "./utils.py:resolve_proxies",
        "must_include": [
            "./utils.py:should_bypass_proxies",
            "./utils.py:get_environ_proxies",
        ],
        "must_not_include": [],
    },
]

FASTAPI_CASES = [
    {
        "desc": "FastAPI.include_router → callee: Router.include_router (via self.router)",
        "changed": "./applications.py:FastAPI.include_router",
        "must_include": ["./routing.py:APIRouter.include_router"],
        "must_not_include": [],
    },
    {
        "desc": "FastAPI.get → callee: Router.get (via self.router)",
        "changed": "./applications.py:FastAPI.get",
        "must_include": ["./routing.py:APIRouter.get"],
        "must_not_include": [],
    },
]

REPO_CONFIGS = {
    "requests": {
        "path_suffixes": [
            "benchmarks/requests/requests/src/requests",
            "requests/src/requests",
        ],
        "cases": REQUESTS_CASES,
    },
    "fastapi": {
        "path_suffixes": [
            "benchmarks/fastapi/fastapi/fastapi",
            "fastapi/fastapi",
        ],
        "cases": FASTAPI_CASES,
    },
}


# ------------------------------------------------------------------ #
#  Eval logic                                                          #
# ------------------------------------------------------------------ #

def find_repo(path_suffixes, base=None):
    """Try to locate repo under base dir."""
    if base is None:
        # try relative to this file, then cwd
        candidates = [os.path.dirname(os.path.dirname(__file__)), os.getcwd()]
    else:
        candidates = [base]

    for root in candidates:
        for suffix in path_suffixes:
            p = os.path.join(root, suffix)
            if os.path.isdir(p):
                return os.path.abspath(p)
    return None


def run_case(graph, case):
    """
    Run one test case.  Returns dict with keys:
      retrieved, must_include, missing, hit_count, hit_total,
      recall, precision_note
    """
    repo_changed = case["changed"]

    # run pipeline
    selected = {repo_changed}
    selected.update(get_blast_radius(graph, repo_changed))
    retrieved = expand_dependencies(graph, list(selected))
    retrieved_set = set(retrieved)

    missing = [m for m in case["must_include"] if m not in retrieved_set]
    hit = [m for m in case["must_include"] if m in retrieved_set]
    recall = len(hit) / len(case["must_include"]) if case["must_include"] else 1.0

    false_positives = [r for r in retrieved if r in case["must_not_include"]]

    return {
        "retrieved": sorted(retrieved),
        "retrieved_count": len(retrieved),
        "must_include": case["must_include"],
        "hit": hit,
        "missing": missing,
        "false_positives": false_positives,
        "recall": recall,
        "pass": recall == 1.0 and len(false_positives) == 0,
    }


def run_all_cases(repo_path, cases, verbose=False):
    """Build graph once, run all cases."""
    print(f"\nBuilding graph for: {repo_path}")
    graph = build_repository_graph(repo_path)
    print(f"  Graph: {len(graph)} nodes, {sum(len(v) for v in graph.values())} edges")

    results = []
    passed = 0
    total = len(cases)

    for i, case in enumerate(cases):
        result = run_case(graph, case)
        status = "[PASS]" if result["pass"] else "[FAIL]"
        if result["pass"]:
            passed += 1

        print(f"\n  {status} Case {i+1}: {case['desc']}")
        print(f"         changed:   {case['changed']}")
        print(f"         retrieved: {result['retrieved_count']} functions")
        print(f"         recall:    {result['recall']:.2f}")

        if not result["pass"]:
            if result["missing"]:
                print(f"         MISSING:   {result['missing']}")
            if result["false_positives"]:
                print(f"         FALSE POS: {result['false_positives']}")

        if verbose and result["retrieved"]:
            print("         all retrieved:")
            for r in result["retrieved"]:
                print(f"           {r}")

        results.append({"case": case["desc"], **result})

    avg_recall = sum(r["recall"] for r in results) / total if total else 0
    print(f"\n  SUMMARY: {passed}/{total} passed  |  avg_recall={avg_recall:.3f}")
    return results, passed, total


def print_graph_stats(repo_path):
    """Extra diagnostics: print graph stats for a repo."""
    graph = build_repository_graph(repo_path)
    in_degree = {}
    out_degree = {}
    for fn, deps in graph.items():
        out_degree[fn] = len(deps)
        for dep in deps:
            in_degree[dep] = in_degree.get(dep, 0) + 1

    # top hubs (most callers)
    print("\n  Top 10 most-called functions (potential noise sources):")
    for fn, cnt in sorted(in_degree.items(), key=lambda x: -x[1])[:10]:
        print(f"    {cnt:4d} callers: {fn}")

    # isolated nodes (nothing calls them, they call nothing)
    isolated = [fn for fn in graph if out_degree.get(fn, 0) == 0 and in_degree.get(fn, 0) == 0]
    print(f"\n  Isolated nodes (no edges): {len(isolated)}")
    return graph


def check_relevance(repo_path, changed_fn, verbose=True):
    """
    Quick relevance check: show exactly what is retrieved for a single changed fn.
    Helps human inspect whether context makes sense.
    """
    graph = build_repository_graph(repo_path)

    # Direct edges
    direct_callees = graph.get(changed_fn, [])
    blast = get_blast_radius(graph, changed_fn)

    print(f"\n=== Relevance check: {changed_fn} ===")
    print(f"  Direct callees ({len(direct_callees)}):")
    for c in direct_callees:
        print(f"    → {c}")

    print(f"  Blast radius ({len(blast)}) — who breaks if this changes:")
    for b in sorted(blast)[:20]:
        print(f"    ← {b}")
    if len(blast) > 20:
        print(f"    ... and {len(blast)-20} more")

    selected = {changed_fn} | set(blast)
    retrieved = expand_dependencies(graph, list(selected))
    print(f"\n  Total context: {len(retrieved)} functions")
    if verbose:
        for r in sorted(retrieved):
            print(f"    {r}")
    return retrieved


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(description="Real repo eval for DiffContext")
    parser.add_argument("--repo", help="Path to repo root to eval")
    parser.add_argument("--kind", choices=["requests", "fastapi"], help="Which ground truth to use")
    parser.add_argument("--all", action="store_true", help="Run all known repos")
    parser.add_argument("--stats", action="store_true", help="Print graph stats")
    parser.add_argument("--check", help="Run relevance check for a single function ID")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--out", default="eval_results.json")
    args = parser.parse_args()

    all_results = {}

    if args.check and args.repo:
        check_relevance(os.path.abspath(args.repo), args.check, verbose=args.verbose)
        return

    if args.stats and args.repo:
        print_graph_stats(os.path.abspath(args.repo))
        return

    if args.repo and args.kind:
        config = REPO_CONFIGS[args.kind]
        results, passed, total = run_all_cases(
            os.path.abspath(args.repo), config["cases"], verbose=args.verbose
        )
        all_results[args.kind] = {"path": args.repo, "passed": passed, "total": total, "cases": results}

    elif args.all or (not args.repo):
        # Try to auto-locate repos
        for kind, config in REPO_CONFIGS.items():
            repo_path = find_repo(config["path_suffixes"])
            if not repo_path:
                print(f"\n[SKIP] {kind}: repo not found (tried: {config['path_suffixes']})")
                continue
            print(f"\n{'='*60}")
            print(f"  REPO: {kind} @ {repo_path}")
            print(f"{'='*60}")
            results, passed, total = run_all_cases(repo_path, config["cases"], verbose=args.verbose)
            all_results[kind] = {"path": repo_path, "passed": passed, "total": total, "cases": results}

    # Overall summary
    if all_results:
        print(f"\n{'='*60}")
        print("  OVERALL RESULTS")
        print(f"{'='*60}")
        grand_pass = sum(r["passed"] for r in all_results.values())
        grand_total = sum(r["total"] for r in all_results.values())
        for kind, r in all_results.items():
            print(f"  {kind}: {r['passed']}/{r['total']}")
        print(f"  TOTAL: {grand_pass}/{grand_total}")

        with open(args.out, "w") as f:
            json.dump(all_results, f, indent=2, default=str)
        print(f"\nResults saved to {args.out}")


if __name__ == "__main__":
    main()
