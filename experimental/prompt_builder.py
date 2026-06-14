def build_prompt(changes):
    lines = []

    lines.append("CHANGES")

    if changes["added"]:
        lines.append("\nAdded:")

        for name in changes["added"]:
            lines.append(f"- {name}")

    if changes["modified"]:
        lines.append("\nModified:")

        for name in changes["modified"]:
            lines.append(f"- {name}")

    lines.append("\nCODE")

    for name, code in changes["added"].items():
        lines.append(code)
        lines.append("")

    for name, code in changes["modified"].items():
        lines.append(code)
        lines.append("")

    return "\n".join(lines)