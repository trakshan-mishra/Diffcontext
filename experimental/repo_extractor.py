from repository_scanner import find_python_files
from extractor import extract_functions


def extract_repository_functions(repo_path):

    repository_functions = {}

    files = find_python_files(repo_path)

    for file in files:

        functions = extract_functions(file)

        repository_functions.update(
            functions
        )

    return repository_functions


if __name__ == "__main__":

    funcs = extract_repository_functions(".")

    for name, data in funcs.items():
        print(
            name,
            "->",
            data["file"]
        )