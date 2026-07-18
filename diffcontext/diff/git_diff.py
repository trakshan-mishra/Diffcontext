"""
git_diff.py — Extract changed files/symbols from git diff output.
"""

import os
import subprocess
from typing import List, Optional, Set


def get_changed_files(
    repo_path: str,
    ref: str = "HEAD~1",
    against: "Optional[str]" = None,
) -> List[str]:
    """
    Get list of Python files changed between `ref` and `against`.

    against=None (default): compare against the WORKING TREE, i.e. include
    uncommitted changes (staged or not). This is `git diff <ref>` with no
    second ref -- the same thing `git status`-style tools show you before
    you commit.
    against="HEAD" (or any other ref): compare two committed snapshots only;
    uncommitted edits are invisible to this mode.

    Returns list of relative paths like ["./src/auth.py", "./api/login.py"]
    """
    repo_path = os.path.abspath(repo_path)
    try:
        cmd = ["git", "diff", "--name-only", "--diff-filter=ACMR", ref]
        if against is not None:
            cmd.append(against)

        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []

        from ..languages import indexable_extensions
        exts = indexable_extensions()

        files = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.endswith(exts):
                files.append("./" + line)
        return files

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def get_changed_lines(
    repo_path: str,
    filepath: str,
    ref: str = "HEAD~1",
    against: "Optional[str]" = None,
) -> Set[int]:
    """
    Get set of changed line numbers for a specific file.

    against=None (default): compare `ref` against the working tree,
    including uncommitted edits. See get_changed_files for details.

    Returns set of 1-indexed line numbers that were added or modified.
    """
    repo_path = os.path.abspath(repo_path)
    try:
        cmd = ["git", "diff", "-U0", ref]
        if against is not None:
            cmd.append(against)
        cmd += ["--", filepath.lstrip("./")]

        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return set()

        changed_lines: Set[int] = set()
        for line in result.stdout.split("\n"):
            if line.startswith("@@"):
                # Parse @@ -old,count +new,count @@
                parts = line.split()
                for part in parts:
                    if part.startswith("+") and "," in part:
                        start_str, count_str = part[1:].split(",", 1)
                        start = int(start_str)
                        count = int(count_str)
                        for i in range(start, start + max(count, 1)):
                            changed_lines.add(i)
                    elif part.startswith("+") and part[1:].isdigit():
                        changed_lines.add(int(part[1:]))

        return changed_lines

    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return set()


def get_patch_text(
    repo_path: str,
    filepath: str,
    ref: str = "HEAD~1",
    against: "Optional[str]" = None,
    context_lines: int = 3,
) -> str:
    """
    Return the raw unified-diff patch text for a single file between
    `ref` and `against` (working tree if against=None).

    Useful for files that no longer parse: you can't get a line-level
    symbol diff out of AST comparison, but the raw patch still shows
    exactly what text changed (e.g. a commented-out `class` line).

    Returns "" if there's no diff, the file/ref doesn't exist, or git fails.
    """
    repo_path = os.path.abspath(repo_path)
    try:
        cmd = ["git", "diff", f"-U{context_lines}", ref]
        if against is not None:
            cmd.append(against)
        cmd += ["--", filepath.lstrip("./")]

        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return ""
        return result.stdout

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def find_changed_symbols(
    repo_path: str,
    symbols: dict,
    ref: str = "HEAD~1",
    against: "Optional[str]" = None,
    broken_files: Optional[List[str]] = None,
    broken_file_patches: Optional[dict] = None,
    known_broken_files: Optional[List[str]] = None,
) -> List[str]:
    """
    Find which symbol IDs are affected by a git diff.

    against=None (default): compares `ref` against the WORKING TREE, so
    uncommitted edits are picked up. Pass against="HEAD" explicitly if you
    only want committed changes.

    Cross-references changed lines with symbol line ranges to find
    exactly which functions/methods were modified.

    `symbols` is the CURRENT index (built from the working tree / `against`
    ref). A changed file that contributes zero entries to `symbols` is
    ambiguous on its own -- it might have failed to parse (SyntaxError), or
    it might just be a file with no function/method definitions (setup.py,
    a constants module, an __init__.py with only imports, etc). To tell
    these apart correctly, pass `known_broken_files` -- the ground-truth
    list from extract_all_symbols()/RepositoryIndex.broken_files, which
    only contains files that actually raised SyntaxError. Only files in
    that list get the broken-file fallback treatment below; any other
    zero-symbol file is treated as a normal (legitimately function-less)
    file and simply contributes no changed symbols.

    For files confirmed broken via `known_broken_files`:
      - the file's relative path is appended to `broken_files` (if provided)
      - we fall back to the symbol IDs that existed in the file at `ref`
        (the prior, presumably-working revision) via `git show`, so the
        change is still reported instead of silently disappearing.
      - if `broken_file_patches` (a dict) is provided, it's populated with
        {relative_file: raw_patch_text} -- the actual unified diff, since a
        broken file can't be symbol-diffed and the patch is the only real
        signal of what changed.
    """
    known_broken = set(known_broken_files or ())
    changed_files = get_changed_files(repo_path, ref, against)
    if not changed_files:
        return []

    changed_symbols = []

    for sym_id, sym in symbols.items():
        sym_file = "./" + os.path.relpath(sym.file, os.path.abspath(repo_path))
        if sym_file not in changed_files:
            continue

        changed_lines = get_changed_lines(repo_path, sym_file, ref, against)
        if not changed_lines:
            continue

        code_lines = sym.code.count("\n") + 1
        sym_lines = set(range(sym.lineno, sym.lineno + code_lines))

        if sym_lines & changed_lines:
            changed_symbols.append(sym_id)

    # Check for deleted symbols or broken files
    for changed_file in changed_files:
        # Handle broken files (SyntaxError)
        if changed_file in known_broken:
            if broken_files is not None and changed_file not in broken_files:
                broken_files.append(changed_file)

            if broken_file_patches is not None:
                broken_file_patches[changed_file] = get_patch_text(
                    repo_path, changed_file, ref, against
                )

            prior_ids = _symbol_ids_at_ref(repo_path, changed_file, ref)
            for p_id in prior_ids:
                if p_id not in changed_symbols:
                    changed_symbols.append(p_id)
            continue

        # Handle valid files: find symbols that existed before but are gone now
        prior_ids = _symbol_ids_at_ref(repo_path, changed_file, ref)
        current_ids = {
            sym_id for sym_id, sym in symbols.items()
            if "./" + os.path.relpath(sym.file, os.path.abspath(repo_path)) == changed_file
        }
        
        deleted_ids = set(prior_ids) - current_ids
        for d_id in deleted_ids:
            if d_id not in changed_symbols:
                changed_symbols.append(d_id)

    return changed_symbols


def _symbol_ids_at_ref(repo_path: str, filepath: str, ref: str) -> List[str]:
    """
    Best-effort: list symbol IDs ("./file.py:Name") that existed in
    `filepath` at git ref `ref`, by parsing the file's contents at that
    revision. Returns [] if the file didn't exist, wasn't valid Python
    at that ref either, or git/parse fails for any reason.
    """
    repo_path = os.path.abspath(repo_path)
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{filepath.lstrip('./')}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []

        import ast as _ast
        try:
            tree = _ast.parse(result.stdout)
        except SyntaxError:
            return []

        class _FuncVisitor(_ast.NodeVisitor):
            def __init__(self):
                self.class_stack = []
                self.names = []

            def visit_ClassDef(self, node):
                self.class_stack.append(node.name)
                self.generic_visit(node)
                self.class_stack.pop()

            def visit_FunctionDef(self, node):
                self._add(node)
                self.generic_visit(node)

            def visit_AsyncFunctionDef(self, node):
                self._add(node)
                self.generic_visit(node)

            def _add(self, node):
                if self.class_stack:
                    self.names.append(f"{self.class_stack[-1]}.{node.name}")
                else:
                    self.names.append(node.name)

        visitor = _FuncVisitor()
        visitor.visit(tree)
        return [f"{filepath}:{name}" for name in visitor.names]

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []