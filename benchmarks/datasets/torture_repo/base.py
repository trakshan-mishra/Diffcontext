class BaseCommand:
    def invoke(self, ctx):
        self.execute(ctx)

    def execute(self, ctx):
        pass
