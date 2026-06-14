from typing import Optional

from routing import Router, State
from helpers import Helper, make_helper


class App:
    def __init__(self, router: Router = None):
        # Case A: annotated assignment (the v0.4.3 baseline case)
        self.router: Router = Router()

        # Case B: factory function, not a class call directly
        self.helper_a = make_helper()

        # Case C: local variable holding a constructor result
        h = Helper()
        self.helper_b = h

        # Case D: constructor param with a type hint, no call at all
        self.router2 = router

        # Case E: `x or Default()` fallback pattern
        self.router3 = router or Router()

        # Case F: subscripted annotation (Optional[X])
        self.opt_router: Optional[Router] = None

        # Two-hop chain target
        self.state = State()

    def setup_a(self):
        self.router.include_router("a")

    def setup_b(self):
        self.helper_a.run()

    def setup_c(self):
        self.helper_b.run()

    def setup_d(self):
        self.router2.include_router("d")

    def setup_e(self):
        self.router3.include_router("e")

    def setup_f(self):
        self.opt_router.include_router("f")

    def setup_two_hop(self):
        self.state.router.include_router("two-hop")
