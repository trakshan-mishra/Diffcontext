#!/bin/bash

echo "🔍 Running complete analysis on all cloned repositories"
echo "=========================================================="

# Create results directory
mkdir -p benchmark_results

# Analyze each repository
for repo in real_benchmarks/*/; do
    repo_name=$(basename "$repo")
    echo ""
    echo "📊 Analyzing: $repo_name"
    echo "-----------------------------------"
    
    # Run your dependency analysis
    python3 -c "
import sys
import json
from pathlib import Path
from dependency_graph import build_dependency_graph
from extractor import extract_functions

repo_path = '$repo'
print(f'  Path: {repo_path}')

# Extract functions
try:
    functions = extract_functions(repo_path, repo_path)
    print(f'  Functions found: {len(functions)}')
except Exception as e:
    print(f'  Error extracting functions: {e}')
    functions = {}

# Build dependency graph
try:
    deps = build_dependency_graph(repo_path)
    print(f'  Dependencies found: {len(deps)}')
    
    # Calculate graph metrics
    total_edges = sum(len(v) for v in deps.values())
    print(f'  Total edges: {total_edges}')
    print(f'  Avg out-degree: {total_edges/len(deps) if deps else 0:.2f}')
except Exception as e:
    print(f'  Error building graph: {e}')

# Save results
results = {'functions': len(functions), 'dependencies': len(deps) if 'deps' in locals() else 0}
with open(f'benchmark_results/{repo_name}_analysis.json', 'w') as f:
    json.dump(results, f, indent=2)
"
done

echo ""
echo "✅ Analysis complete! Results saved to benchmark_results/"
