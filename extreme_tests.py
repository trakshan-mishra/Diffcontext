#!/usr/bin/env python3
"""
Extreme validation tests for DiffContext on completely unfamiliar repos.
Tests repositories that were NOT used during development.
"""

import subprocess
import tempfile
import os
import json
import time
from pathlib import Path

# Real production repos with different characteristics
REAL_REPOS = {
    "fastapi": {
        "url": "https://github.com/fastapi/fastapi",
        "subpath": "fastapi",
        "test_cases": [
            {
                "name": "FastAPI.include_router",
                "changed": "./applications.py:FastAPI.include_router",
                "should_find": ["./routing.py:APIRouter.include_router"],
                "complexity": "high"
            },
            {
                "name": "FastAPI.get",
                "changed": "./applications.py:FastAPI.get",
                "should_find": ["./routing.py:APIRouter.get"],
                "complexity": "high"
            }
        ]
    },
    "django": {
        "url": "https://github.com/django/django",
        "subpath": "django",
        "test_cases": [
            {
                "name": "QuerySet.filter",
                "changed": "./db/models/query.py:QuerySet.filter",
                "should_find": ["./db/models/sql/query.py:Query.add_q"],
                "complexity": "extreme"
            },
            {
                "name": "Model.save",
                "changed": "./db/models/base.py:Model.save",
                "should_find": ["./db/models/base.py:Model.save_base"],
                "complexity": "extreme"
            }
        ]
    },
    "pytest": {
        "url": "https://github.com/pytest-dev/pytest",
        "subpath": "src/_pytest",
        "test_cases": [
            {
                "name": "pytest.main",
                "changed": "./main.py:pytest.main",
                "should_find": ["./config.py:Config._prepareconfig"],
                "complexity": "high"
            }
        ]
    },
    "celery": {
        "url": "https://github.com/celery/celery",
        "subpath": "celery",
        "test_cases": [
            {
                "name": "Celery.task",
                "changed": "./app/base.py:Celery.task",
                "should_find": ["./app/trace.py:build_tracer"],
                "complexity": "extreme"
            }
        ]
    },
    "sqlalchemy": {
        "url": "https://github.com/sqlalchemy/sqlalchemy",
        "subpath": "lib/sqlalchemy",
        "test_cases": [
            {
                "name": "Session.execute",
                "changed": "./orm/session.py:Session.execute",
                "should_find": ["./engine/base.py:Connection.execute"],
                "complexity": "extreme"
            }
        ]
    }
}

def clone_repo(url, subpath):
    """Clone a repository and return the path to the Python package"""
    temp_dir = tempfile.mkdtemp(prefix=f"diffctx_test_")
    print(f"Cloning {url}...")
    
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, temp_dir],
            check=True,
            capture_output=True,
            text=True,
            timeout=120
        )
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Clone failed: {e.stderr[:200]}")
        return None
    
    repo_path = os.path.join(temp_dir, subpath)
    if not os.path.exists(repo_path):
        # Try to find the package
        for root, dirs, files in os.walk(temp_dir):
            if "setup.py" in files or "pyproject.toml" in files:
                # Look for the main package directory
                for d in dirs:
                    if d != "tests" and d != "docs" and d != "examples":
                        potential = os.path.join(root, d)
                        if os.path.exists(os.path.join(potential, "__init__.py")):
                            repo_path = potential
                            break
                break
    
    return repo_path if os.path.exists(repo_path) else None

def analyze_graph_quality(repo_path):
    """Analyze graph quality metrics"""
    from multi_file_dependency_graph import build_repository_graph
    
    print(f"  Building graph...")
    start = time.time()
    graph = build_repository_graph(repo_path)
    build_time = (time.time() - start) * 1000
    
    total_nodes = len(graph)
    total_edges = sum(len(deps) for deps in graph.values())
    avg_degree = total_edges / total_nodes if total_nodes else 0
    
    # Check for isolated nodes
    isolated = sum(1 for deps in graph.values() if len(deps) == 0)
    
    # Check for hub nodes
    in_degree = {}
    for deps in graph.values():
        for dep in deps:
            in_degree[dep] = in_degree.get(dep, 0) + 1
    
    hubs = sorted(in_degree.items(), key=lambda x: -x[1])[:5]
    
    return {
        "nodes": total_nodes,
        "edges": total_edges,
        "avg_degree": avg_degree,
        "isolated_pct": (isolated / total_nodes * 100) if total_nodes else 0,
        "build_time_ms": build_time,
        "top_hubs": [(name.split(":")[-1], count) for name, count in hubs]
    }

def test_repository(repo_name, repo_config):
    """Run extreme tests on a repository"""
    print(f"\n{'='*70}")
    print(f"TESTING: {repo_name.upper()}")
    print(f"{'='*70}")
    
    # Clone repository
    repo_path = clone_repo(repo_config["url"], repo_config["subpath"])
    if not repo_path:
        print(f"❌ Failed to clone {repo_name}")
        return None
    
    print(f"✓ Cloned to: {repo_path}")
    
    # Analyze graph quality
    print(f"\n📊 Graph Quality Analysis:")
    graph_stats = analyze_graph_quality(repo_path)
    print(f"  Nodes: {graph_stats['nodes']:,}")
    print(f"  Edges: {graph_stats['edges']:,}")
    print(f"  Avg degree: {graph_stats['avg_degree']:.2f}")
    print(f"  Isolated nodes: {graph_stats['isolated_pct']:.1f}%")
    print(f"  Build time: {graph_stats['build_time_ms']:.0f}ms")
    
    if graph_stats['avg_degree'] < 0.5:
        print(f"  ⚠️  WARNING: Very sparse graph! (avg degree < 0.5)")
    
    # Run test cases
    print(f"\n🎯 Running Test Cases:")
    results = []
    
    for case in repo_config.get("test_cases", []):
        print(f"\n  Case: {case['name']}")
        print(f"    Changed: {case['changed'].split(':')[-1]}")
        
        try:
            from benchmarks.evaluator import run_diffcontext
            
            # Test different depths
            for depth in [1, 2, 3]:
                result = run_diffcontext(repo_path, [case['changed']], max_depth=depth)
                retrieved_set = set(result)
                
                # Check if it finds expected functions
                found = [exp for exp in case['should_find'] if exp in retrieved_set]
                missing = [exp for exp in case['should_find'] if exp not in retrieved_set]
                
                if found:
                    print(f"      Depth {depth}: ✓ Found {len(found)}/{len(case['should_find'])}")
                    for f in found:
                        print(f"        - {f.split(':')[-1]}")
                    if not missing:
                        results.append({
                            "case": case['name'],
                            "depth": depth,
                            "status": "pass",
                            "found": len(found),
                            "expected": len(case['should_find'])
                        })
                        break
                elif depth == 3:
                    print(f"      Depth {depth}: ❌ Missing all expected dependencies")
                    if missing:
                        for m in missing:
                            print(f"        Expected: {m.split(':')[-1]}")
                    results.append({
                        "case": case['name'],
                        "depth": depth,
                        "status": "fail",
                        "found": 0,
                        "expected": len(case['should_find'])
                    })
        except Exception as e:
            print(f"      ❌ Error: {str(e)[:100]}")
            results.append({
                "case": case['name'],
                "depth": None,
                "status": "error",
                "error": str(e)[:100]
            })
    
    return {
        "repo": repo_name,
        "path": repo_path,
        "stats": graph_stats,
        "results": results
    }

def run_extreme_tests():
    """Run tests on all repositories"""
    all_results = {}
    
    for repo_name, repo_config in REAL_REPOS.items():
        result = test_repository(repo_name, repo_config)
        if result:
            all_results[repo_name] = result
        
        # Cleanup temp directory
        if result and 'path' in result:
            import shutil
            temp_dir = os.path.dirname(result['path'])
            if temp_dir.startswith('/tmp/diffctx_test_'):
                print(f"\n  Cleaning up {temp_dir}")
                shutil.rmtree(temp_dir, ignore_errors=True)
    
    # Final summary
    print(f"\n{'='*70}")
    print("EXTREME TEST SUMMARY")
    print(f"{'='*70}")
    
    total_tests = 0
    passed_tests = 0
    
    for repo_name, result in all_results.items():
        print(f"\n{repo_name.upper()}:")
        print(f"  Graph: {result['stats']['nodes']} nodes, {result['stats']['avg_degree']:.2f} avg degree")
        
        for test_result in result['results']:
            total_tests += 1
            if test_result['status'] == 'pass':
                passed_tests += 1
                print(f"  ✅ {test_result['case']}: PASS (depth {test_result['depth']})")
            else:
                print(f"  ❌ {test_result['case']}: {test_result['status'].upper()}")
    
    print(f"\n{'='*70}")
    print(f"FINAL SCORE: {passed_tests}/{total_tests} tests passed")
    print(f"PASS RATE: {(passed_tests/total_tests*100) if total_tests else 0:.1f}%")
    
    if passed_tests < total_tests:
        print("\n⚠️  WARNING: Low pass rate indicates overfitting to test data!")
        print("   The '100% precision' claim may be specific to your test repos.")
    else:
        print("\n🎉 Excellent! System generalizes to unfamiliar repos.")
    
    return all_results

if __name__ == "__main__":
    print("="*70)
    print("DIFFCONTEXT EXTREME VALIDATION")
    print("Testing on completely unfamiliar production repositories")
    print("="*70)
    
    results = run_extreme_tests()
    
    # Save results
    with open("extreme_test_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\n✓ Detailed results saved to extreme_test_results.json")
