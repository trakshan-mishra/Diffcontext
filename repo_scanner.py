import os

EXCLUDED_DIRS = {
    "__pycache__",
    ".git",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "venv",
    ".venv",
    "env",
    "node_modules",
    "experimental",
    "examples",
    "docs",
    "tests",
    "test",
    "benchmarks",
    "datasets",
    "dist",
    "build",
    "egg-info",
}


def find_python_files(root_dir):
    python_files = []

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [
            d for d in dirs
            if d not in EXCLUDED_DIRS
            and not d.endswith(".egg-info")
        ]

        for file in files:
            if file.endswith(".py"):
                python_files.append(
                    os.path.join(root, file)
                )

    return python_files


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    for f in find_python_files(path):
        print(f)