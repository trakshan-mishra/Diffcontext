import json
import os, time

from benchmarks.evaluator import run_diffcontext
from repo_extractor import extract_repository_functions

DATASETS = [
    "simple_repo",
    "medium_repo"
]

for dataset in DATASETS:

    path = os.path.join(
        "benchmarks",
        "datasets",
        dataset
    )

    with open(
        os.path.join(path, "expected.json")
    ) as f:

        expected_json = json.load(f)

    expected = set(
        expected_json["expected_context"]
    )

    # TEMPORARY
    # Later DiffContext will generate this automatically

    start = time.perf_counter()

    retrieved = set(
    run_diffcontext(
        path,
        expected_json["changed"]
    )
)
    
    elapsed_ms = (
    time.perf_counter() - start
) * 1000
    

    correct = expected.intersection(
        retrieved
    )


    functions = extract_repository_functions(
        path
    )

    total_functions = len(
        functions
)
    

    reduction = (
    1 -
    len(retrieved) /
    total_functions
) * 100
    


    recall = (
        len(correct)
        / len(expected)
    ) * 100

    precision = (
        len(correct)
        / len(retrieved)
    ) * 100
    
    
    print("\n" + "=" * 50)

    print(
        "DATASET:",
        dataset
    )

    print(
        f"Total Functions: {total_functions}"
    )

    print(
        f"Selected: {len(retrieved)}"
    )

    print(
        f"Recall: {recall:.2f}%"
    )

    print(
        f"Precision: {precision:.2f}%"
    )

    print(
        f"Reduction: {reduction:.2f}%"
    )
    print(
    f"Runtime: {elapsed_ms:.2f} ms"
)