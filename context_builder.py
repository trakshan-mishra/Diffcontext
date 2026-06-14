def build_context(functions, selected_functions):
    sections = []

    for func in selected_functions:
        if func not in functions:
            continue

        sections.append(
            f"FUNCTION: {func}\n\n"
            f"{functions[func]}"
        )

    return "\n\n".join(sections)


functions = {
    "add": "def add(a,b):\n    return a+b",

    "multiply": "def multiply(a,b):\n    return a*b",

    "calculate": """def calculate(a,b):
    return add(a,b) + multiply(a,b)"""
}

selected = [
    "calculate",
    "add",
    "multiply"
]

print(build_context(functions, selected))