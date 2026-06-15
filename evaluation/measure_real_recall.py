#!/usr/bin/env python3
"""Measure true recall by manually tracing what Session.get actually does"""

import ast
import inspect
import subprocess
import tempfile

# Manually traced: What functions Session.get actually uses
# Based on reading requests source code v2.31.0
GROUND_TRUTH_GET = {
    # Direct call chain
    "./sessions.py:Session.get": "changed",
    "./sessions.py:Session.request": "direct call",
    "./sessions.py:Session.send": "called by request",
    "./sessions.py:Session.prepare_request": "prepares request",
    "./adapters.py:HTTPAdapter.send": "actual HTTP send",
    
    # Called via kwargs chain
    "./sessions.py:Session.merge_environment_settings": "merges proxies/auth",
    "./utils.py:get_environ_proxies": "gets proxy settings",
    "./utils.py:should_bypass_proxies": "proxy bypass logic",
    "./utils.py:resolve_proxies": "resolves proxy URL",
    
    # Redirect handling  
    "./sessions.py:SessionRedirectMixin.resolve_redirects": "handles redirects",
    "./sessions.py:SessionRedirectMixin.get_redirect_target": "gets redirect URL",
    
    # Cookies
    "./cookies.py:extract_cookies_to_jar": "extracts cookies from response",
    "./cookies.py:merge_cookies": "merges cookie jars",
    
    # Hooks
    "./hooks.py:dispatch_hook": "dispatches response hooks",
    
    # Auth (if used, but get doesn't use directly)
    # So these are NOT required for basic GET
    
    # Total required context: ~12-15 functions
}

def analyze_actual_code():
    """Read requests source to verify dependencies"""
    repo_path = "benchmarks/requests/src/requests"
    
    # Read Session.get
    with open(f"{repo_path}/sessions.py") as f:
        source = f.read()
    
    tree = ast.parse(source)
    
    # Find Session.get
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == 'Session':
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == 'get':
                    print("Session.get dependencies found in AST:")
                    for child in ast.walk(item):
                        if isinstance(child, ast.Call):
                            if isinstance(child.func, ast.Attribute):
                                print(f"  - calls self.{child.func.attr}()")
                            elif isinstance(child.func, ast.Name):
                                print(f"  - calls {child.func.id}()")
                    break

def measure_recall():
    """Compare what graph finds vs ground truth"""
    from multi_file_dependency_graph import build_repository_graph
    from benchmarks.evaluator import run_diffcontext
    
    repo = "benchmarks/requests/src/requests"
    
    # Build graph
    graph = build_repository_graph(repo)
    
    # Test different depths
    for depth in [1, 2, 3, None]:
        result = run_diffcontext(repo, ["./sessions.py:Session.get"], max_depth=depth)
        retrieved_set = set(result)
        
        # Known required functions (simplified)
        required = {
            "./sessions.py:Session.request",
            "./sessions.py:Session.send",
            "./sessions.py:Session.prepare_request",
        }
        
        found = required & retrieved_set
        missing = required - retrieved_set
        
        print(f"\n{'='*60}")
        print(f"Depth: {depth if depth else 'unlimited'}")
        print(f"{'='*60}")
        print(f"Retrieved: {len(retrieved_set)} functions")
        print(f"Found critical: {len(found)}/3")
        
        if missing:
            print(f"MISSING: {missing}")
            
            # Check if these functions exist in graph at all
            for m in missing:
                if m in graph:
                    print(f"  - {m} exists in graph but not retrieved")
                else:
                    print(f"  - {m} NOT in graph (dependency detection failed)")
        
        recall = len(found) / len(required)
        print(f"Recall: {recall:.1%}")

def check_function_exists():
    """Verify which functions are even in the graph"""
    from multi_file_dependency_graph import build_repository_graph
    
    repo = "benchmarks/requests/src/requests"
    graph = build_repository_graph(repo)
    
    critical_functions = [
        "./sessions.py:Session.request",
        "./sessions.py:Session.send", 
        "./sessions.py:Session.prepare_request",
        "./sessions.py:Session.merge_environment_settings",
        "./adapters.py:HTTPAdapter.send",
        "./sessions.py:SessionRedirectMixin.resolve_redirects",
        "./cookies.py:extract_cookies_to_jar",
        "./hooks.py:dispatch_hook",
    ]
    
    print("\nFunction existence in graph:")
    for func in critical_functions:
        exists = func in graph
        status = "✓" if exists else "✗"
        print(f"  {status} {func.split(':')[-1]}")

if __name__ == "__main__":
    print("Analyzing requests Session.get...")
    analyze_actual_code()
    check_function_exists()
    measure_recall()
