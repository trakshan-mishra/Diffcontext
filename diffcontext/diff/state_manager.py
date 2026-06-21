"""
state_manager.py — Snapshot-based change detection (no git required).
"""

import json
import os
from typing import Dict, Iterable, List, Optional

from ..models import DiffResult


def _symbol_file(fn_id: str) -> str:
    """Extract the file portion of a 'file.py:Class.method' symbol id."""
    return fn_id.split(":", 1)[0] if ":" in fn_id else fn_id


def compare_states(
    previous: Dict[str, dict],
    current: Dict[str, dict],
    broken_files: Optional[Iterable[str]] = None,
) -> DiffResult:
    """
    Compare two snapshots of repository functions.

    Each snapshot is: {function_id: {"code": "..."}}

    broken_files: relative paths (e.g. "./objects.py") of files that failed
    to parse (SyntaxError) when building `current`. Symbols that previously
    existed in one of these files are reported as `modified` rather than
    `deleted` -- the function wasn't removed, the file just can't be parsed
    right now. The file itself is also recorded in `broken_files` so callers
    can flag it distinctly from a normal diff.
    """
    broken_files = set(broken_files or ())

    modified = []
    added = []
    deleted = []

    for fn_id in previous:
        if fn_id not in current:
            if _symbol_file(fn_id) in broken_files:
                # File failed to parse this run -- treat every symbol that
                # used to live there as modified (not deleted), since the
                # change (the syntax break) is exactly what needs review.
                modified.append(fn_id)
            else:
                deleted.append(fn_id)
        elif previous[fn_id] != current[fn_id]:
            modified.append(fn_id)

    for fn_id in current:
        if fn_id not in previous:
            added.append(fn_id)

    return DiffResult(
        modified=modified,
        added=added,
        deleted=deleted,
        broken_files=sorted(broken_files),
    )


def save_state(state: Dict, path: str = "diffcontext_state.json"):
    """Save function snapshot to disk."""
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def load_state(path: str = "diffcontext_state.json") -> Dict:
    """Load previous function snapshot from disk."""
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)