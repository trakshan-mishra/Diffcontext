#!/usr/bin/env python3
"""
Test that precision doesn't regress.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from benchmarks.evaluator import run_diffcontext
from multi_file_dependency_graph import build_repository_graph

def test_requests_session_get_precision():
    """Test that Session.get doesn't bring in too many false positives"""
    repo_path = "benchmarks/requests/src/requests"
    
    if not os.path.exists(repo_path):
        print("⚠️ Requests repo not found, skipping test")
        return
    
    graph = build_repository_graph(repo_path)
    result = run_diffcontext(repo_path, ["./sessions.py:Session.get"], max_depth=1)
    
    # Expected: only direct callees and changed function
    expected_min = {"./sessions.py:Session.get", "./sessions.py:Session.request"}
    expected_max = {"./sessions.py:Session.get", "./sessions.py:Session.request", 
                    "./sessions.py:Session.prepare_request"}
    
    retrieved_set = set(result)
    
    # Should contain at least the direct callee
    assert "./sessions.py:Session.request" in retrieved_set, "Missing direct callee"
    
    # Should not contain unrelated utility functions
    unrelated = [f for f in result if "utils.py" in f and "request" not in f]
    assert len(unrelated) < 3, f"Too many unrelated utils: {unrelated}"
    
    print(f"✓ Precision test passed: {len(result)} functions retrieved")
    print(f"  Retrieved: {[f.split(':')[-1] for f in result[:5]]}")

if __name__ == "__main__":
    test_requests_session_get_precision()



    
