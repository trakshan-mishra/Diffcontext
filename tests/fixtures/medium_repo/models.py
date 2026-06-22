"""Data models for the medium_repo fixture."""

class User:
    def __init__(self, name, email):
        self.name = name
        self.email = email

    def display_name(self):
        return self.name.title()


class Order:
    def __init__(self, user, total):
        self.user = user
        self.total = total

    def summary(self):
        return f"{self.user.display_name()}: ${self.total:.2f}"
