from base import BaseCommand


class Command(BaseCommand):
    def main(self, ctx):
        self.invoke(ctx)
