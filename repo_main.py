from repo_extractor import (
    extract_repository_functions
)

from diff import compare_functions

from blast_radius import (
    get_blast_radius
)

from dependency_expander import (
    expand_dependencies
)

from context_builder import (
    build_context
)

from multi_file_dependency_graph import (
    build_repository_graph
)

from state_manager import (
    load_state,
    save_state
)


def main():

    current_state = (
        extract_repository_functions(".")
    )

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

        print(
            "No changes detected"
        )

        return

    graph = build_repository_graph(".")

    selected = set(changed)

    for func in changed:

        selected.update(
            get_blast_radius(
                graph,
                func
            )
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