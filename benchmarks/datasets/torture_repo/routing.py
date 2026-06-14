class Router:
    def include_router(self, other):
        self.add_api_route(other)

    def add_api_route(self, route):
        pass


class State:
    def __init__(self):
        self.router = Router()
