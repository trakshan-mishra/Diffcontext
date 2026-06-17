import json

from multi_file_dependency_graph import build_repository_graph
from dependency_expander import expand_dependencies
from blast_radius import get_blast_radius

REPO = "benchmarks/datasets/torture_repo"

CASE_NAMES = {
    "./app.py:App.setup_a": "A: annotated self.x: Router = Router()",
    "./app.py:App.setup_b": "B: factory function self.x = make_helper()",
    "./app.py:App.setup_c": "C: local var  h = Helper(); self.x = h",
    "./app.py:App.setup_d": "D: typed ctor param  self.x = router",
    "./app.py:App.setup_e": "E: BoolOp  self.x = router or Router()",
    "./app.py:App.setup_f": "F: subscripted ann.  Optional[Router]",
    "./app.py:App.setup_two_hop": "G: two-hop  self.state.router.method()",
    "./commands.py:Command.main": "H: inherited self.method() (cross-file)",
    "./routing.py:Router.include_router": "(sanity) same-file self.method()",
    "./base.py:BaseCommand.invoke": "(sanity) same-file self.method()",
}


def main():
    graph = build_repository_graph(REPO)

    with open(f"{REPO}/expected_edges.json") as f:
        expected = json.load(f)

    print("=== per-case edge check ===")
    tp = fp = fn = 0

    for fid, expected_deps in expected.items():
        actual = set(graph.get(fid, []))
        exp = set(expected_deps)

        case_tp = actual & exp
        case_fp = actual - exp
        case_fn = exp - actual

        tp += len(case_tp)
        fp += len(case_fp)
        fn += len(case_fn)

        status = "PASS" if not case_fp and not case_fn else "FAIL"
        print(f"[{status}] {CASE_NAMES.get(fid, fid)}")
        print(f"        fid:      {fid}")
        print(f"        expected: {sorted(exp)}")
        print(f"        actual:   {sorted(actual)}")
        if case_fp:
            print(f"        EXTRA (false positive): {sorted(case_fp)}")
        if case_fn:
            print(f"        MISSING (false negative): {sorted(case_fn)}")

    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else float("nan")
    )

    print()
    print(f"precision={precision:.2f}  recall={recall:.2f}  f1={f1:.2f}")
    print()

    # ---- collision check: nothing OUTSIDE legacy/ should ever resolve
    # into the decoy Router class (its own internal self-calls are fine) ----
    print("=== collision check (legacy/old_router.py decoy) ===")
    decoy_hit = False
    for fid, deps in graph.items():
        if fid.startswith("./legacy/"):
            continue
        for d in deps:
            if d.startswith("./legacy/old_router.py"):
                decoy_hit = True
                print(f"BAD: {fid} -> {d}")
    if not decoy_hit:
        print("OK: nothing outside legacy/ resolves to the decoy Router class")
    print()

    # ---- depth cap demo on chain.py (f1->f2->f3->f4->f5) ----
    print("=== expand_dependencies depth cap (chain.py) ===")
    unbounded = expand_dependencies(graph, ["./chain.py:f1"])
    capped = expand_dependencies(graph, ["./chain.py:f1"], max_depth=2)
    print(f"max_depth=None : {sorted(unbounded)}")
    print(f"max_depth=2    : {sorted(capped)}")
    print()

    # ---- blast radius sanity (reverse of chain) ----
    print("=== blast radius (chain.py:f5) ===")
    print(sorted(get_blast_radius(graph, "./chain.py:f5")))


if __name__ == "__main__":
    main()
