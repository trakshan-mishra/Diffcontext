"""
Usage:
  python3 graph_diff.py dump <repo_path> <out.json>
  python3 graph_diff.py diff <before.json> <after.json> <repo_path>

Workflow:
  1. git stash (old code)        -> python3 graph_diff.py dump <repo> before.json
  2. git stash pop (new code)    -> python3 graph_diff.py dump <repo> after.json
  3. python3 graph_diff.py diff before.json after.json <repo>

"diff" prints every added/removed edge, plus the source line of the call
that produced it, so you can eyeball whether each new edge is real or a
mis-resolution.
"""
import json
import sys

from multi_file_dependency_graph import build_repository_graph


def dump(repo_path, out_path):
    g = build_repository_graph(repo_path)
    with open(out_path, "w") as f:
        json.dump(g, f, indent=2, sort_keys=True)
    edges = sum(len(v) for v in g.values())
    print(f"{len(g)} nodes, {edges} edges -> {out_path}")


def _src_snippet(repo_path, fid, target_short_name):
    """Find the line in fid's source that mentions target_short_name."""
    rel = fid.split(":", 1)[0]  # "./foo.py"
    path = f"{repo_path}/{rel.lstrip('./')}"
    try:
        with open(path) as f:
            for i, line in enumerate(f, 1):
                if target_short_name in line and "." in line:
                    return f"{rel}:{i}: {line.strip()}"
    except OSError:
        pass
    return None


def diff(before_path, after_path, repo_path):
    before = json.load(open(before_path))
    after = json.load(open(after_path))

    nodes = set(before) | set(after)

    added = 0
    removed = 0

    for fid in sorted(nodes):
        b = set(before.get(fid, []))
        a = set(after.get(fid, []))

        for dep in sorted(a - b):
            added += 1
            short = dep.split(":", 1)[1].split(".")[-1]
            snippet = _src_snippet(repo_path, fid, short)
            print(f"+ {fid}")
            print(f"    -> {dep}")
            if snippet:
                print(f"    src: {snippet}")

        for dep in sorted(b - a):
            removed += 1
            print(f"- {fid}")
            print(f"    -> {dep}  (was here, now gone)")

    print(f"\n{added} edges added, {removed} edges removed")
    print("Check: every '+' points at a real call to that target.")
    print("Check: every '-' was actually wrong before (not a new miss).")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "dump":
        dump(sys.argv[2], sys.argv[3])
    elif cmd == "diff":
        diff(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(__doc__)
