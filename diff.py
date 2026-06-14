def compare_functions(previous_state, current_state):

    modified = {}
    added = {}
    deleted = []

    for fn in previous_state:

        if fn not in current_state:
            deleted.append(fn)

        elif previous_state[fn] != current_state[fn]:
            modified[fn] = current_state[fn]

    for fn in current_state:

        if fn not in previous_state:
            added[fn] = current_state[fn]

    return {
        "modified": modified,
        "added": added,
        "deleted": deleted
    }
