#!/usr/bin/env python3
"""
Test only Python repositories from the real_benchmarks directory
"""
import sys
import json
from pathlib import Path
import importlib.util

# Import your modules
sys.path.insert(0, '.')
from dependency_graph import build_dependency_graph
from extractor import extract_functions

def test_repository(repo_path: Path):
    """Test a single repository"""
    print(f"\n{'='*60}")
    print(f"Testing: {repo_path.name}")
    print(f"{'='*60}")
    
    # Check if it's a Python repo (has .py files)
    py_files = list(repo_path.rglob('*.py'))
    if not py_files:
        print(f"  ⚠️  No Python files found - skipping")
        return None
    
    print(f"  Found {len(py_files)} Python files")
    
    # Test function extraction
    try:
        # Try with directory
        functions = extract_functions(str(repo_path), str(repo_path))
        print(f"  ✅ Functions extracted: {len(functions)}")
    except Exception as e:
        print(f"  ❌ Function extraction failed: {e}")
        functions = {}
    
    # Test graph building
    try:
        deps = build_dependency_graph(str(repo_path))
        print(f"  ✅ Graph built: {len(deps)} nodes")
        
        # Calculate stats
        total_edges = sum(len(v) for v in deps.values())
        avg_degree = total_edges / len(deps) if deps else 0
        print(f"  📊 Total edges: {total_edges}")
        print(f"  📊 Avg out-degree: {avg_degree:.2f}")
        
        return {
            'name': repo_path.name,
            'py_files': len(py_files),
            'functions': len(functions),
            'nodes': len(deps),
            'edges': total_edges,
            'avg_degree': avg_degree
        }
    except Exception as e:
        print(f"  ❌ Graph building failed: {e}")
        return None

def main():
    """Test all Python repositories"""
    real_benchmarks = Path('real_benchmarks')
    
    # Only test repositories that likely contain Python code
    python_repos = ['fastapi', 'django', 'pytest', 'celery', 'sqlalchemy', 'jax', 'anthropic']
    
    results = []
    for repo_name in python_repos:
        repo_path = real_benchmarks / repo_name
        if repo_path.exists():
            result = test_repository(repo_path)
            if result:
                results.append(result)
        else:
            print(f"⚠️  {repo_name} not found - run clone_real_repos.sh first")
    
    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    
    for r in results:
        print(f"\n{r['name'].upper()}:")
        print(f"  Python files: {r['py_files']}")
        print(f"  Functions: {r['functions']}")
        print(f"  Graph nodes: {r['nodes']}")
        print(f"  Graph edges: {r['edges']}")
        print(f"  Avg degree: {r['avg_degree']:.2f}")
    
    # Save results
    with open('python_repos_analysis.json', 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"\n💾 Results saved to python_repos_analysis.json")

if __name__ == "__main__":
    main()
