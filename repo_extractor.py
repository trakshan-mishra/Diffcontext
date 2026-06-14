import os
import sys

from repo_scanner import find_python_files
from extractor import extract_functions


def extract_repository_functions(repo_path):
    repo_path = os.path.abspath(repo_path)
    repository_functions = {}

    for file in find_python_files(repo_path):
        functions = extract_functions(file, repo_path)
        repository_functions.update(functions)

    return repository_functions


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    funcs = extract_repository_functions(path)
    for name in sorted(funcs):
        print(name)
    print(f"\nTotal: {len(funcs)} functions")