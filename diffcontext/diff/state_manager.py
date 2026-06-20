"""
state_manager.py — Snapshot-based change detection (no git required).
"""

import json
import os
from typing import Dict, List

from ..models import DiffResult


def compare_states(
    previous: Dict[str, dict],
    current: Dict[str, dict],
) -> DiffResult:
    """
    Compare two snapshots of repository functions.

    Each snapshot is: {function_id: {"code": "..."}}
    """
    modified = []
    added = []
    deleted = []

    for fn_id in previous:
        if fn_id not in current:
            deleted.append(fn_id)
        elif previous[fn_id] != current[fn_id]:
            modified.append(fn_id)

    for fn_id in current:
        if fn_id not in previous:
            added.append(fn_id)

    return DiffResult(modified=modified, added=added, deleted=deleted)


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
