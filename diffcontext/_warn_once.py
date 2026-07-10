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
from typing import Optional, Set


class WarnState:
    """
    De-dup state for warn-once semantics, scoped to whoever owns it.

    A long-lived process (an agent harness serving many repos/sessions)
    should create one WarnState per indexing session so one session's
    warnings never suppress another's; the module-level default preserves
    the old process-wide behavior for direct callers.
    """

    def __init__(self):
        self.syntax_files: Set[str] = set()
        self.encoding_files: Set[str] = set()
        self._lock = threading.Lock()

    def first_syntax(self, key: str) -> bool:
        with self._lock:
            if key in self.syntax_files:
                return False
            self.syntax_files.add(key)
            return True

    def first_encoding(self, key: str) -> bool:
        with self._lock:
            if key in self.encoding_files:
                return False
            self.encoding_files.add(key)
            return True

    def reset(self) -> None:
        with self._lock:
            self.syntax_files.clear()
            self.encoding_files.clear()


_default_state = WarnState()


def warn_syntax_error_once(
    logger: logging.Logger,
    filename: str,
    exc: SyntaxError,
    state: Optional[WarnState] = None,
) -> None:
    """Log a SyntaxError warning for `filename`, but only the first time it's
    seen by `state` (the process-wide default when None)."""
    key = os.path.abspath(filename)
    if not (state or _default_state).first_syntax(key):
        return

    logger.warning(
        "\033[93mSkipping %s due to SyntaxError: %s (line %s)\033[0m",
        os.path.basename(filename), exc.msg, exc.lineno,
    )


def check_and_warn_encoding(
    logger: logging.Logger,
    filename: str,
    raw_bytes: bytes,
    state: Optional[WarnState] = None,
) -> None:
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
        if not (state or _default_state).first_encoding(key):
            return

        logger.warning(
            "\033[93m%s is not valid UTF-8 (%s at byte %d) -- invalid bytes "
            "will be silently dropped, which can corrupt string literals "
            "in the extracted code\033[0m",
            os.path.basename(filename), e.reason, e.start,
        )


def reset_warned_files() -> None:
    """Clear the process-wide de-dup caches. Mainly useful for tests."""
    _default_state.reset()