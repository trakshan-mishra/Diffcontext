# DiffContext v0.2.0 Benchmark Results

## simple_repo

* Recall: 100.00%
* Precision: 100.00%
* Reduction: 0.00%
* Runtime: 0.75 ms

## medium_repo

* Recall: 33.33%
* Precision: 100.00%
* Reduction: 75.00%
* Runtime: 0.49 ms

## Observations

* Local dependency analysis works correctly.
* Blast radius propagation works correctly.
* Dependency expansion works correctly.
* Cross-file import resolution is not implemented.
* Medium repository benchmark exposes the current limitation.

## Next Milestone

v0.3.0 — Import Resolution

Goal: Increase medium_repo recall from 33.33% toward 100%.
