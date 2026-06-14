from impact_scorer import compute_scores
from dependency_graph import build_dependency_graph


def select_top_functions(graph, top_k=3):

    scores = compute_scores(graph)

    ranked = sorted(
        scores.items(),
        key=lambda x: x[1]["impact_score"],
        reverse=True
    )

    return ranked[:top_k]


if __name__ == "__main__":

    graph = build_dependency_graph("app.py")

    selected = select_top_functions(
        graph,
        top_k=3
    )

    for func, score in selected:
        print(
            func,
            score["impact_score"]
        )