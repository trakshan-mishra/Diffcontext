def build_context(functions, selected_functions):

    sections = []

    for func in selected_functions:

        if func not in functions:
            continue

        sections.append(
            f"FUNCTION: {func}\n\n"
            f"{functions[func]['code']}"
        )

    return "\n\n".join(sections)
