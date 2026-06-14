from dependency_graph import build_dependency_graph
from blast_radius import get_blast_radius
from impact_scorer import compute_scores

graph = build_dependency_graph("app.py")
scores = compute_scores(graph)

changed_function = "add"


if __name__ == "__main__":

    print("=" * 40)
    print("CHANGE REPORT")
    print("=" * 40)

    print(f"Changed Function: {changed_function}")

    print(
        f"Impact Score: "
        f"{scores[changed_function]['impact_score']}"
    )

    print("\nAffected Functions:")

    for fn in get_blast_radius(graph, changed_function):
        print("-", fn)