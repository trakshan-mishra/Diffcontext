"""Consumer using from-imports against the src-layout package,
including a name that only exists in mypkg/__init__.py as a re-export.
"""

from mypkg import top_fn, core_fn
from mypkg.core import core_fn as core_direct


def call_from_import():
    return top_fn() + core_fn()


def call_direct():
    return core_direct()
