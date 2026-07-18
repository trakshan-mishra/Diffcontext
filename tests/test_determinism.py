#!/usr/bin/env python3
"""
tests/test_determinism.py — the dependency graph must be byte-identical
across interpreter hash seeds.

Edge *order* feeds everything downstream: representative picks in Phases
1C-1E, tie-breaks in scoring, and ultimately which symbol makes it into a
tight token budget. A graph that differs per process is a reproducibility
hazard for the benchmark and makes agent-loop results non-repeatable.

Hash randomization can't be changed in-process, so each build runs in a
subprocess with its own PYTHONHASHSEED.
"""

import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIUM = os.path.join(BASE, "tests", "fixtures", "medium_repo")

# json.dumps with sort_keys normalizes dict key order but preserves list
# order — so equality compares exactly what we care about: edge order.
_SNIPPET = (
    "import sys, json;"
    "sys.path.insert(0, sys.argv[1]);"
    "from diffcontext.graph_builder import build_repository_graph;"
    "print(json.dumps(build_repository_graph(sys.argv[2]), sort_keys=True))"
)


def _graph_json(seed: str) -> str:
    env = dict(os.environ, PYTHONHASHSEED=seed)
    result = subprocess.run(
        [sys.executable, "-c", _SNIPPET, BASE, MEDIUM],
        env=env, capture_output=True, text=True, check=True,
    )
    return result.stdout


def test_graph_byte_identical_across_hash_seeds():
    seeds = ("0", "1", "4242")
    graphs = {seed: _graph_json(seed) for seed in seeds}
    assert graphs["0"], "subprocess produced no graph output"
    assert graphs["0"] == graphs["1"] == graphs["4242"], (
        "dependency graph differs between hash seeds — some edge-emitting "
        "phase is iterating a set or hash-ordered dict"
    )


def test_graph_edge_lists_have_no_duplicates():
    graph = json.loads(_graph_json("0"))
    for fid, edges in graph.items():
        assert len(edges) == len(set(edges)), f"duplicate edges for {fid}"
