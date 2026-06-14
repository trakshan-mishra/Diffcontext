import os


def find_python_files(root_dir):

    python_files = []

    for root, dirs, files in os.walk(root_dir):

        dirs[:] = [
            d
            for d in dirs
            if d not in [
                "__pycache__",
                ".git",
                "experimental",
                "examples",
                "venv"
            ]
        ]

        for file in files:

            if file.endswith(".py"):

                python_files.append(
                    os.path.join(root, file)
                )

    return python_files


if __name__ == "__main__":

    for f in find_python_files("."):
        print(f)