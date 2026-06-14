# DiffContext

Static-analysis-based context compiler for LLM-assisted software engineering.

## Overview

Large software repositories contain thousands of files and millions of tokens. While Large Language Models (LLMs) excel at reasoning, debugging, and code generation, they struggle with identifying relevant context from large codebases.

DiffContext addresses this problem by transforming code changes into targeted LLM-ready context using dependency analysis, blast radius propagation, impact scoring, and context compilation.

Instead of sending an entire repository to an LLM, DiffContext identifies the most relevant code required for reasoning.

---

## Problem

Traditional workflows often provide excessive repository context to LLMs.

This results in:

* Higher token costs
* Increased latency
* More irrelevant information
* Reduced reasoning quality

Key questions:

* What changed?
* What depends on it?
* What is affected?
* What code should be shown to the LLM?

---

## Architecture

Repository
↓
AST Extraction
↓
State Tracking
↓
Diff Detection
↓
Dependency Graph
↓
Blast Radius Analysis
↓
Impact Scoring
↓
Dependency Expansion
↓
Context Builder
↓
Compiled Context
↓
LLM

---

## Features

### AST Extraction

Uses Python's AST module to extract function definitions.

### State Tracking

Maintains repository state for change detection.

### Diff Detection

Identifies added, modified, deleted, and unchanged functions.

### Dependency Graph

Builds function-level dependency relationships.

### Blast Radius Analysis

Determines which functions are affected by a code change.

### Impact Scoring

Ranks changed functions according to their potential repository impact.

Current scoring formula:

```python
impact_score = (
    blast_radius * 3 +
    indegree * 2 +
    outdegree
)
```

Where:

* blast_radius = number of affected downstream functions
* indegree = number of callers
* outdegree = number of dependencies

### Dependency Expansion

Includes required dependencies needed for reasoning.

### Context Compilation

Produces targeted LLM-ready context.

---

## Example

### Dependency Graph

```python
{
    "add": [],
    "multiply": [],
    "calculate": ["add", "multiply"],
    "report": ["calculate"]
}
```

### Blast Radius

```python
get_blast_radius(graph, "add")

# Output
["calculate", "report"]
```

### Compiled Context

```python
FUNCTION: calculate

def calculate():
    x = add(2, 3)
    y = multiply(4, 5)
    return x + y

FUNCTION: add

def add(a, b):
    return a + b + 1

FUNCTION: multiply

def multiply(a, b):
    return a * b
```

---

## Repository Structure

```text
app.py
blast_radius.py
change_context_compiler.py
compile_context.py
context_builder.py
context_selector.py
dependency_expander.py
dependency_graph.py
diff.py
hash_utils.py
impact_report.py
impact_scorer.py
prompt_builder.py
state_manager.py
```

---

## Current Status

Implemented:

* AST Extraction
* State Tracking
* Diff Detection
* Dependency Graph Generation
* Blast Radius Analysis
* Impact Scoring
* Dependency Expansion
* Context Compilation

---

## Future Work

* Multi-file repository support
* Import resolution
* Class dependency analysis
* Automatic context ranking
* Large-scale repository benchmarking

---

## License

MIT License
