from repo_extractor import (
    extract_repository_functions
)

def build_context(
    functions,
    selected_functions
):

    sections = []

    for function_id in selected_functions:

        if function_id not in functions:
            continue

        file_name, function_name = (
            function_id.split(":", 1)
        )

        sections.append(
            f"FILE: {file_name}\n"
            f"FUNCTION: {function_name}\n\n"
            f"{functions[function_id]['code']}"
        )

    return "\n\n".join(sections)


if __name__ == "__main__":

    functions = extract_repository_functions(".")

    selected = [
        "./app.py:report",
        "./app.py:calculate",
        "./app.py:add",
        "./app.py:multiply"
    ]

    print(
        build_context(
            functions,
            selected
        )
    )