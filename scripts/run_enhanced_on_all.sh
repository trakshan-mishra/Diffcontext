#!/bin/bash

echo "🚀 Running enhanced analyzer on all Python repositories"
echo "=========================================================="

# List of Python repositories
repos=("fastapi" "django" "pytest" "celery" "sqlalchemy" "jax" "anthropic")

for repo in "${repos[@]}"; do
    repo_path="real_benchmarks/$repo"
    
    if [ -d "$repo_path" ]; then
        echo ""
        echo "📊 Analyzing: $repo"
        echo "-----------------------------------"
        python3 enhanced_dependency_graph.py "$repo_path"
    else
        echo "⚠️  $repo not found in real_benchmarks/"
    fi
done

echo ""
echo "✅ Enhanced analysis complete!"
echo "📁 Results saved to *_enhanced_analysis.json files"
