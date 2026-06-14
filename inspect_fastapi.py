# inspect_fastapi.py

from repo_extractor import extract_repository_functions

functions = extract_repository_functions(
    "benchmarks/fastapi/fastapi"
)

for fn in sorted(functions):
    if (
        "__call__" in fn
        or "include_router" in fn
        or "add_api_route" in fn
        or "api_route" in fn
    ):
        print(fn)

print("\nTotal:", len(functions))