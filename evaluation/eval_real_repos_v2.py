"""
eval_real_repos.py  (v2 — fixed path discovery, added online repo support)
===========================================================================
Fixes:
  - REPO_CONFIGS path_suffixes corrected (requests lives at requests/src/requests, not benchmarks/)
  - check_relevance now prints a warning if the graph is empty (bad path)
  - Added ONLINE_REPOS dict for testing against real GitHub repos on demand

Usage:
    # local repos (auto-discover)
    python eval_real_repos.py --all

    # correct check path
    python eval_real_repos.py \
      --repo requests/src/requests \
      --check ./sessions.py:Session.send --verbose

    # clone + eval a real GitHub repo
    python eval_real_repos.py --online django
    python eval_real_repos.py --online flask
    python eval_real_repos.py --online black
"""

import sys
import os
import argparse
import json
import tempfile
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from multi_file_dependency_graph import build_repository_graph
from blast_radius import get_blast_radius
from dependency_expander import expand_dependencies


# ------------------------------------------------------------------ #
#  Ground truth cases (manually verified 1-hop neighborhoods)         #
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
        "desc": "Session.send → blast radius must include Session.get, Session.post",
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

# ------------------------------------------------------------------ #
#  Repo configs — FIXED: correct path suffixes                        #
# ------------------------------------------------------------------ #

REPO_CONFIGS = {
    "requests": {
        # requests library is bundled in the project at requests/src/requests/
        "path_suffixes": [
            "requests/src/requests",           # correct local path
            "benchmarks/requests/src/requests",  # if ever moved
        ],
        "cases": REQUESTS_CASES,
    },
    "fastapi": {
        "path_suffixes": [
            "benchmarks/fastapi/fastapi/fastapi",
            "fastapi/fastapi/fastapi",
        ],
        "cases": [],  # add cases once fastapi is available
    },
}

# ------------------------------------------------------------------ #
#  Online repos to clone on demand                                     #
# ------------------------------------------------------------------ #

ONLINE_REPOS = {
    "django": {
        "url": "https://github.com/django/django",
        # sub-path inside cloned repo where the Python package lives
        "subpath": "django",
        # spot-check: ORM query.py calls sql/compiler.py
        "spot_checks": [
            "./db/models/query.py:QuerySet.filter",
            "./db/models/query.py:QuerySet.count",
        ],
    },
    "flask": {
        "url": "https://github.com/pallets/flask",
        "subpath": "src/flask",
        "spot_checks": [
            "./app.py:Flask.route",
            "./app.py:Flask.make_response",
        ],
    },
    "black": {
        "url": "https://github.com/psf/black",
        "subpath": "src/black",
        "spot_checks": [
            "./linegen.py:transform_line",
            "./mode.py:Mode.__post_init__",
        ],
    },
    "mypy": {
        "url": "https://github.com/python/mypy",
        "subpath": "mypy",
        "spot_checks": [
            "./checker.py:TypeChecker.check_func_def",
            "./nodes.py:FuncDef.__init__",
        ],
    },
    "httpx": {
        "url": "https://github.com/encode/httpx",
        "subpath": "httpx",
        "spot_checks": [
            "./_client.py:Client.get",
            "./_client.py:Client.send",
        ],
    },
}


# ------------------------------------------------------------------ #
#  Core eval logic                                                      #
# ------------------------------------------------------------------ #

def run_case(graph, case):
    repo_changed = case["changed"]
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
    print(f"\nBuilding graph for: {repo_path}")
    graph = build_repository_graph(repo_path)
    node_count = len(graph)
    edge_count = sum(len(v) for v in graph.values())
    print(f"  Graph: {node_count} nodes, {edge_count} edges")

    if node_count == 0:
        print("  ERROR: empty graph — check repo path!")
        return [], 0, 0

    results = []
    passed = 0

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

        if verbose:
            for r in result["retrieved"]:
                print(f"           {r}")

        results.append({"case": case["desc"], **result})

    total = len(cases)
    avg_recall = sum(r["recall"] for r in results) / total if total else 0
    print(f"\n  SUMMARY: {passed}/{total} passed  |  avg_recall={avg_recall:.3f}")
    return results, passed, total


def check_relevance(repo_path, changed_fn, verbose=True):
    """Show exactly what DiffContext retrieves for one function. Human-readable."""
    print(f"\nBuilding graph for: {repo_path}")
    graph = build_repository_graph(repo_path)
    node_count = len(graph)
    print(f"  Graph: {node_count} nodes, {sum(len(v) for v in graph.values())} edges")

    if node_count == 0:
        print("  ERROR: empty graph — check repo path!")
        return []

    if changed_fn not in graph:
        print(f"  WARNING: '{changed_fn}' not found in graph!")
        print("  Available functions matching pattern:")
        key_part = changed_fn.split(":")[-1]
        for fn in sorted(graph):
            if key_part.lower() in fn.lower():
                print(f"    {fn}")
        return []

    direct_callees = graph.get(changed_fn, [])
    blast = get_blast_radius(graph, changed_fn)

    print(f"\n=== Relevance check: {changed_fn} ===")
    print(f"  Direct callees ({len(direct_callees)}) — what this fn calls:")
    for c in direct_callees:
        print(f"    → {c}")

    print(f"\n  Blast radius ({len(blast)}) — who breaks if this changes:")
    for b in sorted(blast)[:20]:
        print(f"    ← {b}")
    if len(blast) > 20:
        print(f"    ... and {len(blast)-20} more")

    selected = {changed_fn} | set(blast)
    retrieved = expand_dependencies(graph, list(selected))
    print(f"\n  Total context: {len(retrieved)} functions  "
          f"(changed + blast radius + their deps)")
    if verbose:
        for r in sorted(retrieved):
            marker = "(*)" if r == changed_fn else "   "
            print(f"    {marker} {r}")
    return retrieved


def spot_check_online(name, config, verbose=False):
    """Clone a real GitHub repo and run spot checks on it."""
    tmp = tempfile.mkdtemp(prefix=f"diffctx_{name}_")
    url = config["url"]
    subpath = config["subpath"]
    print(f"\nCloning {url} ...")
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", url, tmp],
            check=True, capture_output=True
        )
    except subprocess.CalledProcessError as e:
        print(f"  Clone failed: {e.stderr.decode()[:200]}")
        return

    repo_path = os.path.join(tmp, subpath)
    if not os.path.isdir(repo_path):
        print(f"  ERROR: subpath '{subpath}' not found in cloned repo")
        return

    print(f"Building graph for: {repo_path}")
    graph = build_repository_graph(repo_path)
    node_count = len(graph)
    edge_count = sum(len(v) for v in graph.values())
    print(f"  Graph: {node_count} nodes, {edge_count} edges")

    if node_count == 0:
        print("  ERROR: empty graph")
        return

    for fn in config["spot_checks"]:
        print(f"\n  Spot check: {fn}")
        if fn not in graph:
            print(f"    NOT FOUND in graph (fn ID may differ between versions)")
            # show close matches
            key = fn.split(":")[-1].lower()
            matches = [k for k in graph if key in k.lower()][:5]
            if matches:
                print(f"    Close matches: {matches}")
            continue

        callees = graph.get(fn, [])
        blast = get_blast_radius(graph, fn)
        selected = {fn} | set(blast)
        retrieved = expand_dependencies(graph, list(selected))

        print(f"    Callees: {len(callees)}, Blast: {len(blast)}, Context: {len(retrieved)} fns")
        if callees:
            print(f"    First callee: {callees[0]}")
        if blast:
            print(f"    First in blast: {sorted(blast)[0]}")

    # compute graph hub stats
    in_degree = {}
    for deps in graph.values():
        for d in deps:
            in_degree[d] = in_degree.get(d, 0) + 1
    top = sorted(in_degree.items(), key=lambda x: -x[1])[:5]
    print(f"\n  Top 5 hubs (most callers — potential context pollution):")
    for fn, cnt in top:
        print(f"    {cnt:4d} callers: {fn}")

    # cleanup
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def find_repo(path_suffixes, base=None):
    candidates = [os.getcwd()]
    if base:
        candidates.insert(0, base)
    # also try parent dirs
    cwd = os.getcwd()
    for _ in range(3):
        candidates.append(cwd)
        cwd = os.path.dirname(cwd)

    for root in candidates:
        for suffix in path_suffixes:
            p = os.path.join(root, suffix)
            if os.path.isdir(p):
                return os.path.abspath(p)
    return None


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", help="Path to repo package root")
    p.add_argument("--kind", choices=list(REPO_CONFIGS.keys()))
    p.add_argument("--all", action="store_true", help="Run all local repos")
    p.add_argument("--online", choices=list(ONLINE_REPOS.keys()),
                   help="Clone + spot-check a real GitHub repo")
    p.add_argument("--check", help="Relevance check for one function ID")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--out", default="eval_results.json")
    args = p.parse_args()

    all_results = {}

    if args.online:
        spot_check_online(args.online, ONLINE_REPOS[args.online], verbose=args.verbose)
        return

    if args.check:
        repo = os.path.abspath(args.repo) if args.repo else find_repo(
            REPO_CONFIGS.get(args.kind, {}).get("path_suffixes", [])
        )
        if not repo:
            print("ERROR: --repo required for --check")
            sys.exit(1)
        check_relevance(repo, args.check, verbose=args.verbose)
        return

    if args.repo and args.kind:
        config = REPO_CONFIGS[args.kind]
        results, passed, total = run_all_cases(
            os.path.abspath(args.repo), config["cases"], verbose=args.verbose
        )
        all_results[args.kind] = {"path": args.repo, "passed": passed, "total": total}

    elif args.all or not args.repo:
        for kind, config in REPO_CONFIGS.items():
            repo_path = find_repo(config["path_suffixes"])
            if not repo_path:
                print(f"\n[SKIP] {kind}: not found (tried: {config['path_suffixes']})")
                continue
            if not config["cases"]:
                print(f"\n[SKIP] {kind}: no test cases defined yet")
                continue
            print(f"\n{'='*60}")
            print(f"  REPO: {kind} @ {repo_path}")
            print(f"{'='*60}")
            results, passed, total = run_all_cases(repo_path, config["cases"], verbose=args.verbose)
            all_results[kind] = {"path": repo_path, "passed": passed, "total": total}

    if all_results:
        grand_pass = sum(r["passed"] for r in all_results.values())
        grand_total = sum(r["total"] for r in all_results.values())
        print(f"\n{'='*60}")
        print("  OVERALL")
        print(f"{'='*60}")
        for kind, r in all_results.items():
            print(f"  {kind}: {r['passed']}/{r['total']}")
        print(f"  TOTAL: {grand_pass}/{grand_total}")
        with open(args.out, "w") as f:
            json.dump(all_results, f, indent=2)
        print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
