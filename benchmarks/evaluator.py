from repo_extractor import extract_repository_functions
from multi_file_dependency_graph import build_repository_graph
from blast_radius import get_blast_radius
from dependency_expander import expand_dependencies


def run_diffcontext(repo_path, changed):

    functions = extract_repository_functions(
        repo_path
    )

    graph = build_repository_graph(
        repo_path
    )



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

    return expanded


