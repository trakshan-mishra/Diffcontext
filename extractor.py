import ast


def extract_functions(filename):

    with open(filename, "r") as f:
        source = f.read()

    tree = ast.parse(source)

    functions = {}

    for node in tree.body:

        if isinstance(node, ast.FunctionDef):

            function_id = f"{filename}:{node.name}"

            functions[function_id] = {
                "file": filename,
                "code": ast.get_source_segment(
                    source,
                    node
                )
            }

    return functions