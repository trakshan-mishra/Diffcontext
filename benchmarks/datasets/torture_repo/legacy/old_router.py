class Router:
    def include_router(self, other):
        self.legacy_merge(other)

    def legacy_merge(self, other):
        pass
