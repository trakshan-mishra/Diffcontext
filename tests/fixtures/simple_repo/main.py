"""A minimal single-file program: one function calling another."""

def greet(name):
    message = build_message(name)
    print(message)

def build_message(name):
    return f"Hello, {name}!"

if __name__ == "__main__":
    greet("World")
