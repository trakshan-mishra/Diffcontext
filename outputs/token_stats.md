# Token Statistics Example

## Repository

Functions: 100

Approximate Repository Tokens:

4000

---

## Selected Context

Functions:

* add
* calculate
* report

Approximate Context Tokens:

600

---

## Results

Repository Tokens:

4000

Selected Context Tokens:

600

Reduction:

85%

---

## Observation

DiffContext reduces the amount of code sent to an LLM by selecting functions affected by a change through dependency analysis and blast radius propagation.

Rather than sending an entire repository, DiffContext constructs a targeted context package containing only relevant functions and their dependencies.

This improves context efficiency while preserving reasoning-critical information.
