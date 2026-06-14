import ast
from prompt_builder import build_prompt
from state_manager import load_state
from state_manager import save_state

def extract_functions(filename):
    with open(filename, "r") as f:
        source = f.read()

    tree = ast.parse(source)

    functions = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            functions[node.name] = ast.get_source_segment(source, node)

    return functions



previous_state = load_state()
current_state = extract_functions("app.py")


print("PREVIOUS STATE:")
print(previous_state)

print("\nCURRENT STATE:")
print(current_state)

modified = {}
added = {}
deleted = []
unchanged = []

for fn in previous_state:

    if fn not in current_state:
        deleted.append(fn)

    elif previous_state[fn] != current_state[fn]:
        modified[fn] = current_state[fn]

    else:
        unchanged.append(fn)

for fn in current_state:

    if fn not in previous_state:
        added[fn] = current_state[fn]

diff_result = {
    "modified": modified,
    "added": added,
    "deleted": deleted,
    "unchanged": unchanged
}

print("\nCHANGES:")
print(diff_result)

prompt = build_prompt(diff_result)

print("\nPROMPT:")
print(prompt)

save_state(current_state)