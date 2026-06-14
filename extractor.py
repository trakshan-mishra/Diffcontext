import ast
import os


class FunctionCollector(ast.NodeVisitor):
    def __init__(self):
        self.class_stack = []
        self.collected = []

    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)

        for child in node.body:
            self.visit(child)

        self.class_stack.pop()

    def visit_FunctionDef(self, node):
        # Build ownership-aware name
        if self.class_stack:
            function_name = ".".join(self.class_stack) + "." + node.name
        else:
            function_name = node.name

        self.collected.append((function_name, node))

        # Continue traversal for nested functions
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        # Handle async functions too
        if self.class_stack:
            function_name = ".".join(self.class_stack) + "." + node.name
        else:
            function_name = node.name

        self.collected.append((function_name, node))

        self.generic_visit(node)


def extract_functions(filename, repo_path):
    with open(filename, "r", encoding="utf-8", errors="ignore") as f:
        source = f.read()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    functions = {}

    relative_file = "./" + os.path.relpath(filename, repo_path)

    collector = FunctionCollector()
    collector.visit(tree)

    for function_name, node in collector.collected:

        function_id = f"{relative_file}:{function_name}"

        code = ast.get_source_segment(source, node)

        if code is None:
            continue

        functions[function_id] = {
            "file": filename,
            "code": code,
        }

    return functions