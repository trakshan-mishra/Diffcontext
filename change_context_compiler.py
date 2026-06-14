functions = extract_functions("app.py")

diff = compare_functions(old_state, functions)

changed = list(diff["modified"].keys())

graph = build_dependency_graph("app.py")

selected = set(changed)

for func in changed:
    affected = get_blast_radius(graph, func)
    selected.update(affected)

expanded = expand_dependencies(
    graph,
    list(selected)
)

context = build_context(
    functions,
    expanded
)

print(context)