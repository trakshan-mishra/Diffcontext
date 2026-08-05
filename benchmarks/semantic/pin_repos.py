#!/usr/bin/env python3
"""
pin_repos.py — clone the eval corpus at the EXACT commits it was mined from.

The embedding cache is keyed by symbol id (`./path.py:name`) at a repo's HEAD.
Cloning a newer HEAD gives different symbols, so vectors silently fail to join
against the mined pairs and the ablation scores a shrunken corpus without
saying so. This script pins every repo to the SHA in repo_pins.json, which is
what makes a GPU box (Colab/Kaggle) reproduce the local corpus exactly.

  python -m benchmarks.semantic.pin_repos                 # clone/checkout all
  python -m benchmarks.semantic.pin_repos --check         # verify, exit 1 on drift
  python -m benchmarks.semantic.pin_repos --write         # re-pin to current HEADs
  python -m benchmarks.semantic.pin_repos click flask     # subset

Cloning is shallow-ish: full history is not needed to embed HEAD symbols, but
the mining scripts DO need history, so this fetches everything by default. Pass
--depth 1 when you only intend to embed.
"""

import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
PINS_PATH = os.path.join(_HERE, "repo_pins.json")
REPOS_DIR = os.path.join(_ROOT, "benchmark_repos")


def load_pins() -> dict:
    with open(PINS_PATH, encoding="utf-8") as f:
        return json.load(f)["repos"]


def _git(*args: str, cwd: str = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def head_of(path: str) -> str:
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def ensure(name: str, url: str, sha: str, repos_dir: str, depth: int = 0) -> bool:
    """Clone if absent, then hard-checkout the pinned SHA. -> True if at the pin."""
    path = os.path.join(repos_dir, name)
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(repos_dir, exist_ok=True)
        cmd = ["clone", url, path]
        if depth:
            # A shallow clone cannot reach an arbitrary older SHA, so fetch the
            # one commit we actually want rather than guessing a depth.
            cmd = ["clone", "--filter=blob:none", url, path]
        print(f"  cloning {name} …", flush=True)
        r = _git(*cmd)
        if r.returncode:
            print(f"  !! clone failed for {name}: {r.stderr.strip()[:200]}")
            return False
    if head_of(path) != sha:
        if _git("cat-file", "-e", sha + "^{commit}", cwd=path).returncode:
            _git("fetch", "--all", "--tags", cwd=path)
        r = _git("checkout", "--force", sha, cwd=path)
        if r.returncode:
            print(f"  !! cannot check out {sha[:10]} in {name}: "
                  f"{r.stderr.strip()[:200]}")
            return False
    ok = head_of(path) == sha
    print(f"  {name:11s} {'OK  ' if ok else 'DRIFT'} {head_of(path)[:10]} "
          f"(want {sha[:10]})")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="*", help="subset of repo names (default: all)")
    ap.add_argument("--repos-dir", default=REPOS_DIR)
    ap.add_argument("--check", action="store_true",
                    help="verify pins without cloning; exit 1 on drift")
    ap.add_argument("--write", action="store_true",
                    help="re-pin repo_pins.json to the CURRENT local HEADs")
    ap.add_argument("--depth", type=int, default=0,
                    help="use a blobless clone (faster; embedding-only)")
    args = ap.parse_args()

    pins = load_pins()
    names = args.repos or sorted(pins)
    unknown = [n for n in names if n not in pins]
    if unknown:
        sys.exit(f"unknown repo(s): {', '.join(unknown)}; known: {', '.join(sorted(pins))}")

    if args.write:
        for n in names:
            path = os.path.join(args.repos_dir, n)
            if os.path.isdir(os.path.join(path, ".git")):
                pins[n]["sha"] = head_of(path)
                print(f"  pinned {n} -> {pins[n]['sha'][:10]}")
        with open(PINS_PATH, encoding="utf-8") as f:
            doc = json.load(f)
        doc["repos"] = pins
        with open(PINS_PATH, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        print(f"wrote {os.path.relpath(PINS_PATH, _ROOT)}")
        return

    if args.check:
        drift = []
        for n in names:
            path = os.path.join(args.repos_dir, n)
            cur = head_of(path) if os.path.isdir(os.path.join(path, ".git")) else "(absent)"
            ok = cur == pins[n]["sha"]
            print(f"  {n:11s} {'OK  ' if ok else 'DRIFT'} {cur[:10]} "
                  f"(want {pins[n]['sha'][:10]})")
            if not ok:
                drift.append(n)
        if drift:
            sys.exit(f"\n{len(drift)} repo(s) off their pin: {', '.join(drift)}. "
                     f"Run without --check to fix.")
        print("\nall repos at their pinned commits")
        return

    bad = [n for n in names
           if not ensure(n, pins[n]["url"], pins[n]["sha"], args.repos_dir, args.depth)]
    if bad:
        sys.exit(f"\nfailed to pin: {', '.join(bad)}")
    print(f"\n{len(names)} repo(s) at their pinned commits -> {args.repos_dir}")


if __name__ == "__main__":
    main()
