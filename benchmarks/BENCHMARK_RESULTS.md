# DiffContext Real Repository Benchmarks

## Requests — api.request

Repository: requests

Changed Function:
./api.py:request

Results:

* Total Functions: 228
* Retrieved Functions: 8
* Token Reduction: 95.9%
* Function Reduction: 96.5%
* Runtime: ~220 ms

Notes:

* Retrieved HTTP method wrappers:

  * delete
  * get
  * head
  * options
  * patch
  * post
  * put
  * request

Observation:

* Excellent reduction for a high-level API entry point.
* Retrieved set closely matches expected blast radius.

---

## Requests — sessions.send

Repository: requests

Changed Function:
./sessions.py:send

Results:

* Total Functions: 228
* Retrieved Functions: 47
* Token Reduction: 71.6%
* Function Reduction: 79.4%
* Runtime: ~220 ms

Observation:

* Deep execution-path benchmark.
* Retrieved hooks, cookies, redirects, adapters, and utility functions.
* Demonstrates cross-file dependency traversal.

---

## Click — Command.main

Repository: click

Changed Function:
./core.py:Command.main

Results:

* Total Functions: 506
* Retrieved Functions: 30
* Context Tokens: 8,020
* Full Repository Tokens: 85,367
* Token Reduction: 90.61%
* Function Reduction: 94.07%
* Runtime: 553.8 ms

Key Retrieved Functions:

* Command.main
* Command.invoke
* Command.make_context
* Command.parse_args
* Command.make_parser
* Command._main_shell_completion
* _detect_program_name
* _expand_args
* echo
* shell_complete

Observation:

* First benchmark after ownership-aware call resolution.
* Correctly follows:
  Command.main → Command.make_context → Command.parse_args → Command.make_parser
* Demonstrates class-aware dependency retrieval.

---

## Flask — Flask.dispatch_request

Repository: flask

Changed Function:
./app.py:Flask.dispatch_request

Results:

* Total Functions: 354
* Retrieved Functions: 17
* Context Tokens: 5,667
* Full Repository Tokens: 67,148
* Token Reduction: 91.6%
* Function Reduction: 95.2%
* Runtime: 261.5 ms

Key Retrieved Functions:

* Flask.dispatch_request
* Flask.full_dispatch_request
* Flask.finalize_request
* Flask.preprocess_request
* Flask.handle_user_exception
* Flask.handle_http_exception
* Flask.handle_exception
* Flask.ensure_sync
* Flask.wsgi_app

Observation:

* Retrieved core Flask request lifecycle.
* Demonstrates ownership-aware traversal across request handling flow.
* Strong reduction while preserving high-level request processing context.

---

## Summary

| Repository | Entry Point            | Functions | Retrieved | Token Reduction |  Runtime |
| ---------- | ---------------------- | --------: | --------: | --------------: | -------: |
| requests   | api.request            |       228 |         8 |           95.9% |   220 ms |
| requests   | sessions.send          |       228 |        47 |           71.6% |   220 ms |
| click      | Command.main           |       506 |        30 |          90.61% | 553.8 ms |
| flask      | Flask.dispatch_request |       354 |        17 |           91.6% | 261.5 ms |

Current Status:

* v0.4.1: Class Ownership Extraction
* v0.4.2: Ownership-Aware Call Resolution

Next Goal:

* FastAPI benchmarks
* Dependency Recall evaluation
* SWE-Bench preparation
