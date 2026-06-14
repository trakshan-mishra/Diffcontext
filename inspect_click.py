# inspect_click.py

from repo_extractor import extract_repository_functions

functions = extract_repository_functions(
    "benchmarks/click/src/click"
)

for fn in sorted(functions):
    if (
        "invoke" in fn
        or "parse_args" in fn
        or "main" in fn
    ):
        print(fn)

print("\nTotal:", len(functions))