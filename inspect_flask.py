from repo_extractor import extract_repository_functions

functions = extract_repository_functions(
    "benchmarks/flask/src/flask"
)

for fn in sorted(functions):
    if (
        "dispatch_request" in fn
        or "full_dispatch_request" in fn
        or "wsgi_app" in fn
        or "run" in fn
    ):
        print(fn)

print("\nTotal:", len(functions))