from extractor import extract_functions
from diff import compare_functions

from dependency_graph import build_dependency_graph
from blast_radius import get_blast_radius
from dependency_expander import expand_dependencies

from context_builder import build_context

from state_manager import load_state
from state_manager import save_state


def main():

    current_state = extract_functions("app.py")

    previous_state = load_state()

    diff = compare_functions(
        previous_state,
        current_state
    )

    changed = (
        list(diff["modified"].keys())
        + list(diff["added"].keys())
    )

    if not changed:
        print("No changes detected")
        return

    graph = build_dependency_graph("app.py")

    selected = set(changed)

    for fn in changed:
        selected.update(
            get_blast_radius(graph, fn)
        )

    expanded = expand_dependencies(
        graph,
        list(selected)
    )

    context = build_context(
        current_state,
        expanded
    )

    print(context)

    save_state(current_state)


if __name__ == "__main__":
    main()
