import os


def find_python_files(root_dir):
    python_files = []

    for root, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith(".py"):
                python_files.append(
                    os.path.join(root, file)
                )

    return python_files


if __name__ == "__main__":
    files = find_python_files(".")

    for f in files:
        print(f)