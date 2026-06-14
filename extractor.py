import ast


def extract_functions(filename):

    with open(filename, "r") as f:
        source = f.read()

    tree = ast.parse(source)

    functions = {}

    for node in ast.walk(tree):

        if isinstance(node, ast.FunctionDef):

            functions[node.name] = {
                "file": filename,
                "code": ast.get_source_segment(
                    source,
                    node
                )
            }

    return functions