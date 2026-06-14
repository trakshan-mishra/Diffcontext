from blast_radius import get_blast_radius


def compute_indegree(graph):
    indegree = {node: 0 for node in graph}

    for node in graph:
        for dep in graph[node]:
            indegree[dep] += 1

    return indegree


def compute_outdegree(graph):
    return {
        node: len(graph[node])
        for node in graph
    }


def compute_scores(graph):

    indegree = compute_indegree(graph)
    outdegree = compute_outdegree(graph)

    scores = {}

    for node in graph:

        blast = len(
            get_blast_radius(graph, node)
        )

        score = (
            blast * 3
            + indegree[node] * 2
            + outdegree[node]
        )

        scores[node] = {
            "blast_radius": blast,
            "indegree": indegree[node],
            "outdegree": outdegree[node],
            "impact_score": score
        }

    return scores


from dependency_graph import build_dependency_graph

if __name__ == "__main__":

    graph = build_dependency_graph("app.py")

    scores = compute_scores(graph)

    print(scores)