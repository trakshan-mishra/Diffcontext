def expand_dependencies(graph, selected_functions):
    visited = set()
    result = []

    def dfs(func):
        if func in visited:
            return

        visited.add(func)
        result.append(func)

        for dep in graph.get(func, []):
            dfs(dep)

    for func in selected_functions:
        dfs(func)

    return result


graph = {
    "add": [],
    "multiply": [],
    "calculate": ["add", "multiply"],
    "report": ["calculate"]
}

selected = ["calculate"]


if __name__ == "__main__":
    print(expand_dependencies(graph, selected))