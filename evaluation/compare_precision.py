#!/usr/bin/env python3
"""
Compare precision across different depth settings.
"""

import subprocess
import json
import tempfile
import os

def run_benchmark(depth, max_tokens):
    """Run benchmark with specific settings"""
    cmd = [
        "python3", "benchmark_runner.py",
        "--repo", "benchmarks/requests/src/requests",
        "--changed", "./sessions.py:Session.get",
        "--name", f"depth_{depth}",
        "--max-depth", str(depth),
        "--max-tokens", str(max_tokens),
        "--out", f"results_depth_{depth}.json"
    ]
    
    print(f"\n--- Running depth={depth}, tokens={max_tokens} ---")
    subprocess.run(cmd)
    
    with open(f"results_depth_{depth}.json") as f:
        return json.load(f)

def analyze_results():
    """Compare results from different depths"""
    depths = [1, 2, 3, 0]  # 0 means unlimited
    results = {}
    
    for depth in depths:
        max_tokens = 5000 if depth > 0 else 0
        results[depth] = run_benchmark(depth, max_tokens)
    
    print("\n" + "="*60)
    print("PRECISION COMPARISON REPORT")
    print("="*60)
    
    for depth, data in results.items():
        depth_label = "unlimited" if depth == 0 else str(depth)
        print(f"\nDepth {depth_label}:")
        print(f"  Retrieved: {data['retrieved_count']} functions")
        print(f"  Token reduction: {data['token_reduction_pct']:.1f}%")
        print(f"  Runtime: {data['runtime_ms']:.1f}ms")
        
        # Show first 5 retrieved
        print(f"  First 5 retrieved:")
        for fid in data['retrieved_ids'][:5]:
            # Extract just the function name for readability
            fn_name = fid.split(':')[-1].split('.')[-1]
            print(f"    - {fn_name}")

if __name__ == "__main__":
    analyze_results()