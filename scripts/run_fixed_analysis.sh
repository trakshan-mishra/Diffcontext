#!/bin/bash

echo "🔍 Running fixed analysis on all repositories"
echo "=========================================================="

# Create results directory
mkdir -p benchmark_results

# Analyze each repository - but TensorFlow and TypeScript are not Python!
# We should focus on Python repos only
for repo in fastapi django pytest celery sqlalchemy; do
    repo_path="real_benchmarks/$repo"
    
    if [ -d "$repo_path" ]; then
        echo ""
        echo "📊 Analyzing: $repo"
        echo "-----------------------------------"
        
        # Run enhanced dependency graph
        python3 enhanced_dependency_graph.py "$repo_path"
    fi
done

echo ""
echo "✅ Analysis complete!"
