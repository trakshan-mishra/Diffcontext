"""Package __init__ that both defines symbols and re-exports from submodules.

Mirrors the layout of real PyPI packages (black, flask): the project keeps
its importable packages under src/, and the package __init__ re-exports
implementation functions — one via an absolute import (like black's
`from black.parsing import ...`) and one via a relative import (like
flask's `from .app import Flask`).
"""

from mypkg.core import core_fn  # absolute re-export
from .helpers import helper_fn  # relative re-export


def top_fn():
    return core_fn() + helper_fn()
