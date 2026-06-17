#!/usr/bin/env python3
"""
Enhanced Dependency Graph for Real-World Codebases
Handles: decorators, inheritance, dynamic imports, method chaining
"""
import ast
import json
import sys
from pathlib import Path
from typing import Dict, Set, Tuple, List, Optional
from collections import defaultdict

class RealWorldDependencyExtractor(ast.NodeVisitor):
    """Extracts dependencies from real-world Python codebases"""
    
    def __init__(self, filepath: str, repo_root: Path):
        self.filepath = Path(filepath)
        self.repo_root = repo_root
        self.current_class = None
        self.current_function = None
        self.current_method = None
        self.imports = {}  # alias -> module
        self.from_imports = {}  # name -> module
        self.class_inheritance = []  # (child, parent)
        self.calls = []  # (caller, callee)
        self.decorator_calls = []  # (function, decorator)
        self.functions = []  # list of function names
        
    def visit_ClassDef(self, node):
        old_class = self.current_class
        self.current_class = node.name
        
        # Track inheritance
        for base in node.bases:
            if isinstance(base, ast.Name):
                self.class_inheritance.append((node.name, base.id))
            elif isinstance(base, ast.Attribute):
                # Handle module.Class inheritance
                parts = []
                current = base
                while isinstance(current, ast.Attribute):
                    parts.append(current.attr)
                    current = current.value
                if isinstance(current, ast.Name):
                    parts.append(current.id)
                parent = '.'.join(reversed(parts))
                self.class_inheritance.append((node.name, parent))
        
        self.generic_visit(node)
        self.current_class = old_class
    
    def visit_FunctionDef(self, node):
        self._visit_function(node)
    
    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)
    
    def _visit_function(self, node):
        old_function = self.current_function
        old_method = self.current_method
        
        # Build full function path
        if self.current_class:
            self.current_method = node.name
            self.current_function = f"{self.filepath}::{self.current_class}.{node.name}"
        else:
            self.current_function = f"{self.filepath}::{node.name}"
        
        # Add to functions list
        self.functions.append(self.current_function)
        
        # Track decorators (critical for FastAPI, Flask, etc.)
        for decorator in node.decorator_list:
            decorator_name = self._extract_name(decorator)
            if decorator_name:
                self.decorator_calls.append((self.current_function, decorator_name))
                # Also add as a regular call
                self.calls.append((self.current_function, decorator_name))
        
        self.generic_visit(node)
        self.current_function = old_function
        self.current_method = old_method
    
    def visit_Call(self, node):
        if self.current_function:
            callee = self._extract_callee(node)
            if callee:
                self.calls.append((self.current_function, callee))
        
        # Also check for nested calls
        self.generic_visit(node)
    
    def visit_Import(self, node):
        for alias in node.names:
            self.imports[alias.asname or alias.name] = alias.name
        self.generic_visit(node)
    
    def visit_ImportFrom(self, node):
        module = node.module or ''
        for alias in node.names:
            if alias.name == '*':
                # Handle wildcard imports
                self.from_imports['*'] = module
            else:
                self.from_imports[alias.asname or alias.name] = f"{module}.{alias.name}" if module else alias.name
        self.generic_visit(node)
    
    def visit_Attribute(self, node):
        # Track method calls on objects (e.g., self.router.include_router)
        if self.current_function:
            full_name = self._extract_attribute_chain(node)
            if full_name:
                # This might be a method call on an object
                self.calls.append((self.current_function, full_name))
        self.generic_visit(node)
    
    def _extract_name(self, node) -> Optional[str]:
        """Extract name from AST node"""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return self._extract_attribute_chain(node)
        elif isinstance(node, ast.Call):
            return self._extract_name(node.func)
        return None
    
    def _extract_callee(self, call_node) -> Optional[str]:
        """Extract the function being called"""
        if isinstance(call_node.func, ast.Name):
            # Simple function call
            func_name = call_node.func.id
            
            # Check if it's an imported function
            if func_name in self.from_imports:
                return self.from_imports[func_name]
            return func_name
            
        elif isinstance(call_node.func, ast.Attribute):
            # Method call or attribute access
            return self._extract_attribute_chain(call_node.func)
            
        elif isinstance(call_node.func, ast.Call):
            # Result of another call
            return self._extract_callee(call_node.func)
        
        return None
    
    def _extract_attribute_chain(self, attr_node) -> str:
        """Extract full attribute chain (e.g., self.router.include_router)"""
        parts = []
        current = attr_node
        
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        
        if isinstance(current, ast.Name):
            parts.append(current.id)
        elif isinstance(current, ast.Call):
            # Handle chained calls: obj.method().another()
            parts.append(str(current))
        
        # Reverse to get correct order
        chain = '.'.join(reversed(parts))
        
        # Handle self.method pattern
        if chain.startswith('self.'):
            if self.current_class:
                # Convert self.method to Class.method
                return f"{self.current_class}.{chain[5:]}"
        
        return chain
    
    def get_results(self) -> Dict:
        """Return all extracted data"""
        return {
            'calls': self.calls,
            'decorator_calls': self.decorator_calls,
            'inheritance': self.class_inheritance,
            'imports': self.imports,
            'from_imports': self.from_imports,
            'functions': self.functions
        }


def build_enhanced_graph(repo_path: str, exclude_dirs: List[str] = None):
    """Build enhanced dependency graph for a repository"""
    if exclude_dirs is None:
        exclude_dirs = ['test', 'tests', '__pycache__', 'site-packages', 'venv', 'env', 'build', 'dist']
    
    repo_root = Path(repo_path)
    all_calls = []
    all_inheritance = []
    all_decorators = []
    all_functions = []
    stats = {
        'files_processed': 0,
        'errors': 0,
        'total_calls': 0,
        'total_decorators': 0,
        'total_functions': 0
    }
    
    for py_file in repo_root.rglob('*.py'):
        # Skip excluded directories
        skip = False
        for excluded in exclude_dirs:
            if excluded in str(py_file).lower():
                skip = True
                break
        if skip:
            continue
        
        try:
            with open(py_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            tree = ast.parse(content)
            extractor = RealWorldDependencyExtractor(str(py_file), repo_root)
            extractor.visit(tree)
            
            results = extractor.get_results()
            all_calls.extend(results['calls'])
            all_inheritance.extend(results['inheritance'])
            all_decorators.extend(results['decorator_calls'])
            all_functions.extend(results['functions'])
            stats['files_processed'] += 1
            stats['total_calls'] += len(results['calls'])
            stats['total_decorators'] += len(results['decorator_calls'])
            stats['total_functions'] += len(results['functions'])
            
        except Exception as e:
            stats['errors'] += 1
            continue
    
    return {
        'calls': all_calls,
        'inheritance': all_inheritance,
        'decorators': all_decorators,
        'functions': all_functions,
        'stats': stats
    }


def analyze_repository(repo_path: str):
    """Complete analysis of a repository"""
    print(f"\n📊 Analyzing: {repo_path}")
    print("="*50)
    
    graph = build_enhanced_graph(repo_path)
    
    print(f"Files processed: {graph['stats']['files_processed']}")
    print(f"Errors: {graph['stats']['errors']}")
    print(f"Total functions: {graph['stats']['total_functions']}")
    print(f"Total function calls: {graph['stats']['total_calls']}")
    print(f"Total decorators: {graph['stats']['total_decorators']}")
    print(f"Inheritance relationships: {len(graph['inheritance'])}")
    
    # Calculate call graph density
    unique_callers = len(set(c[0] for c in graph['calls']))
    unique_callees = len(set(c[1] for c in graph['calls']))
    
    print(f"\nCall Graph Stats:")
    print(f"  Unique callers: {unique_callers}")
    print(f"  Unique callees: {unique_callees}")
    print(f"  Call density: {graph['stats']['total_calls'] / max(unique_callers, 1):.2f} calls/caller")
    
    # Show sample of decorators (critical for FastAPI)
    if graph['decorators']:
        print(f"\nSample decorators (first 5):")
        for decorator in graph['decorators'][:5]:
            print(f"  {decorator[0]} -> @{decorator[1]}")
    
    # Show sample of inheritance
    if graph['inheritance']:
        print(f"\nSample inheritance (first 5):")
        for child, parent in graph['inheritance'][:5]:
            print(f"  {child} inherits from {parent}")
    
    return graph


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        repo_path = sys.argv[1]
        if not Path(repo_path).exists():
            print(f"Error: Path {repo_path} does not exist")
            sys.exit(1)
        
        results = analyze_repository(repo_path)
        
        # Save results
        output_file = f"{Path(repo_path).name}_enhanced_analysis.json"
        
        # Convert to serializable format
        json_results = {
            'stats': results['stats'],
            'inheritance': results['inheritance'],
            'decorators': results['decorators'][:100],  # Limit for file size
            'functions_count': len(results['functions'])
        }
        
        with open(output_file, 'w') as f:
            json.dump(json_results, f, indent=2, default=str)
        print(f"\n💾 Results saved to {output_file}")
    else:
        # Analyze all real repos
        benchmarks_dir = Path('real_benchmarks')
        if benchmarks_dir.exists():
            for repo_path in benchmarks_dir.iterdir():
                if repo_path.is_dir():
                    # Skip non-Python repos
                    py_files = list(repo_path.rglob('*.py'))
                    if len(py_files) > 10:  # Only analyze repos with substantial Python code
                        analyze_repository(str(repo_path))
                        print("\n" + "="*50)
        else:
            print("No real_benchmarks directory found. Run clone_python_repos.sh first.")
