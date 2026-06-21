"""
_warn_once.py — De-duplicate repeated warnings for the same file.

parser.py, graph_builder.py, and resolver.py each independently call
ast.parse() on every file. When a file has a syntax error, all three would
otherwise log their own identical warning. This module tracks which files
have already been warned about (per process run) so only the first one
actually prints.

Also covers invalid-UTF-8 source files: these are read with
errors="ignore", which silently DROPS any byte that isn't valid UTF-8 with
no warning, anywhere. A dropped byte inside a string literal corrupts that
literal's contents (e.g. "Café Menu" -> "Caf Menu") without raising any
error or appearing in any log -- the file still parses fine, the symbol
still extracts fine, the code text is just silently wrong. warn_encoding_
issue_once exists so this corruption is at least visible once per file.
"""

import logging
import os
import threading
from typing import Set

_warned_files: Set[str] = set()
_warned_encoding_files: Set[str] = set()
_lock = threading.Lock()


def warn_syntax_error_once(logger: logging.Logger, filename: str, exc: SyntaxError) -> None:
    """Log a SyntaxError warning for `filename`, but only the first time it's seen."""
    key = os.path.abspath(filename)
    with _lock:
        if key in _warned_files:
            return
        _warned_files.add(key)

    logger.warning(
        "\033[93mSkipping %s due to SyntaxError: %s (line %s)\033[0m",
        os.path.basename(filename), exc.msg, exc.lineno,
    )


def check_and_warn_encoding(logger: logging.Logger, filename: str, raw_bytes: bytes) -> None:
    """
    Check whether `raw_bytes` is valid UTF-8. If not, warn once per file --
    the caller will go on to decode with errors="ignore", which silently
    drops the offending bytes (and anything that decoded incorrectly around
    them). This doesn't stop processing; it just makes the data loss
    visible instead of completely silent.
    """
    try:
        raw_bytes.decode("utf-8")
        return
    except UnicodeDecodeError as e:
        key = os.path.abspath(filename)
        with _lock:
            if key in _warned_encoding_files:
                return
            _warned_encoding_files.add(key)

        logger.warning(
            "\033[93m%s is not valid UTF-8 (%s at byte %d) -- invalid bytes "
            "will be silently dropped, which can corrupt string literals "
            "in the extracted code\033[0m",
            os.path.basename(filename), e.reason, e.start,
        )


def reset_warned_files() -> None:
    """Clear the de-dup caches. Mainly useful for tests."""
    with _lock:
        _warned_files.clear()
        _warned_encoding_files.clear()