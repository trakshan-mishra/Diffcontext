"""Consumer package calling into mypkg via bare module imports —
the `import black` / `black.format_file_contents(...)` pattern.
"""

import mypkg
import mypkg.core


def use_top():
    return mypkg.top_fn()


def use_absolute_reexport():
    return mypkg.core_fn()


def use_relative_reexport():
    return mypkg.helper_fn()


def use_dotted_module_call():
    return mypkg.core.core_fn()


def run(task, items):
    return [task(i) for i in items]


def use_function_ref_as_argument():
    # The blackd pattern: partial(black.format_file_contents, ...) — the
    # target function is never *called* here, only referenced.
    return run(mypkg.core_fn, [1, 2])


def use_keyword_function_ref(records):
    return sorted(records, key=mypkg.top_fn)


def local_fn():
    return 3


# NOTE: the padding functions below keep local_fn more than WINDOW_SIZE(=3)
# definitions away from its referrers, so the tests assert real
# function-reference edges rather than sliding-window co-location edges.

def _pad_a():
    return 0


def _pad_b():
    return 0


def _pad_c():
    return 0


def _pad_d():
    return 0


def use_local_function_ref():
    return run(local_fn, [1])


def no_edge_for_shadowing_param(local_fn):
    # `local_fn` here is a parameter, not the module-level function above —
    # passing it along must NOT create an edge to local_fn.
    return run(local_fn, [2])
