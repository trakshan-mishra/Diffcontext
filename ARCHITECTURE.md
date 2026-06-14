# DiffContext Architecture

## High-Level Flow

```text
Repository
    │
    ▼
AST Extraction
    │
    ▼
State Tracking
    │
    ▼
Diff Detection
    │
    ▼
Dependency Graph
    │
    ▼
Blast Radius Analysis
    │
    ▼
Impact Scoring
    │
    ▼
Dependency Expansion
    │
    ▼
Context Builder
    │
    ▼
Compiled Context
    │
    ▼
LLM
```

---

## Stage 1: AST Extraction

Extracts function definitions using Python's AST module.

Output:

```python
{
    "add": "source_code",
    "multiply": "source_code"
}
```

---

## Stage 2: State Tracking

Maintains historical repository state.

Purpose:

* Detect modifications
* Detect additions
* Detect deletions

---

## Stage 3: Diff Detection

Compares current repository state against previous state.

Output:

```python
{
    "modified": {},
    "added": {},
    "deleted": [],
    "unchanged": []
}
```

---

## Stage 4: Dependency Graph

Builds function-level dependencies.

Example:

```python
{
    "calculate": [
        "add",
        "multiply"
    ]
}
```

---

## Stage 5: Blast Radius Analysis

Determines downstream impact.

Example:

```python
get_blast_radius(graph, "add")

# Output
[
    "calculate",
    "report"
]
```

---

## Stage 6: Impact Scoring

Measures repository impact.

Formula:

```python
impact_score =
(blast_radius * 3) +
(indegree * 2) +
(outdegree)
```

Example:

```python
{
    "add": 8,
    "multiply": 8,
    "calculate": 7,
    "report": 2
}
```

---

## Stage 7: Dependency Expansion

Ensures required dependencies are included.

Input:

```python
[
    "calculate"
]
```

Output:

```python
[
    "calculate",
    "add",
    "multiply"
]
```

---

## Stage 8: Context Builder

Constructs LLM-readable context.

Output:

```python
FUNCTION: calculate

def calculate():
    ...

FUNCTION: add

def add():
    ...
```

---

## Stage 9: Context Compilation

Produces final context package for LLM consumption.

Goal:

```text
Entire Repository
        ↓
Relevant Functions
        ↓
Compiled Context
        ↓
LLM
```

DiffContext performs context selection rather than context compression.
