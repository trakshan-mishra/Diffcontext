from extractor import extract_functions
from dependency_graph import build_dependency_graph

from context_selector import select_top_functions
from dependency_expander import expand_dependencies
from context_builder import build_context


def compile_context(filename):
    functions = extract_functions(filename)

    graph = build_dependency_graph(filename)

    top_functions = select_top_functions(
        graph,
        top_k=3
    )

    selected = [
        func
        for func, score in top_functions
    ]

    expanded = expand_dependencies(
        graph,
        selected
    )

    context = build_context(
        functions,
        expanded
    )

    return context


if __name__ == "__main__":
    context = compile_context("app.py")

    print(context)