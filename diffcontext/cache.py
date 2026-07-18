"""
cache.py — SQLite-backed persistent caching for AST parsed symbols and the
repository call graph.
"""

import hashlib
import json
import sqlite3
from typing import Dict, Callable, List, Optional, Tuple

from .models import Symbol


def get_file_hash(filepath: str) -> str:
    """Compute SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        # Python files are small enough to read into memory safely
        hasher.update(f.read())
    return hasher.hexdigest()


def hash_source(source_bytes: bytes) -> str:
    """SHA-256 of already-read file contents (avoids a second disk read)."""
    return hashlib.sha256(source_bytes).hexdigest()


def repo_state_hash(file_hashes: Dict[str, str]) -> str:
    """
    Single hash summarizing the content state of every Python file in the
    repo. Keyed on (relative_path, content_hash) pairs, order-independent.
    Any file added, removed, or edited changes this hash.
    """
    hasher = hashlib.sha256()
    for path in sorted(file_hashes):
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(file_hashes[path].encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


class SymbolCache:
    """
    Persistent SQLite cache for parsed AST symbols and the call graph.

    Safe for concurrent use from multiple threads within one process: the
    connection is created with check_same_thread=False and every public
    operation holds an internal lock (SQLite serializes at the file level
    across processes on its own via WAL).
    """

    def __init__(self, db_path: str = ".diffcontext_cache.db"):
        import threading
        self.db_path = db_path
        self._conn = None
        self._lock = threading.RLock()
        self._connect()

    def _connect(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Cache contents are rebuildable from source; NORMAL skips the
            # per-commit fsync (a measurable cost at one commit per file on
            # a cold index) and risks nothing worse than a stale cache row
            # after power loss, which the content hash then invalidates.
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_db()

    def close(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _init_db(self):
        with self._conn:
            self._conn.executescript('''
                CREATE TABLE IF NOT EXISTS files (
                    file_path TEXT PRIMARY KEY,
                    file_hash TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS symbols (
                    id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    code TEXT NOT NULL,
                    lineno INTEGER,
                    FOREIGN KEY(file_path) REFERENCES files(file_path) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);

                CREATE TABLE IF NOT EXISTS graphs (
                    state_hash   TEXT PRIMARY KEY,
                    graph_json   TEXT NOT NULL,
                    broken_json  TEXT NOT NULL,
                    created_at   INTEGER
                );
            ''')

    # ── Graph caching ─────────────────────────────────────────────────────
    # The call graph is repo-global (cross-file edges), so it is cached as a
    # whole, keyed by repo_state_hash(): the combined content hash of every
    # Python file. Same pattern as symbols — content-addressed, no TTL logic.

    _GRAPH_CACHE_KEEP = 5   # most-recent graph snapshots retained per db

    def get_graph(self, state_hash: str) -> "Optional[Tuple[Dict[str, List[str]], List[str]]]":
        """Return (graph, broken_files) for this exact repo state, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT graph_json, broken_json FROM graphs WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0]), json.loads(row[1])

    def put_graph(
        self,
        state_hash: str,
        graph: Dict[str, List[str]],
        broken_files: "List[str]",
    ) -> None:
        """Persist the graph for this repo state; prune old snapshots."""
        import time as _time
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO graphs VALUES (?, ?, ?, ?)",
                (state_hash, json.dumps(graph), json.dumps(broken_files),
                 int(_time.time())),
            )
            self._conn.execute(
                """DELETE FROM graphs WHERE state_hash NOT IN (
                       SELECT state_hash FROM graphs
                       ORDER BY created_at DESC, rowid DESC LIMIT ?
                   )""",
                (self._GRAPH_CACHE_KEEP,),
            )

    def get_or_parse(
        self,
        filepath: str,
        parse_fn: Callable[[str], Dict[str, Symbol]],
        known_hash: "Optional[str]" = None,
    ) -> Dict[str, Symbol]:
        """
        Return cached symbols if file hash matches, otherwise parse and persist.

        `known_hash` lets a caller that already read and hashed the file
        (the pipeline hashes every file for the repo state hash) skip a
        second full disk read here. It MUST be the hash of the file's
        current contents.
        """
        file_hash = known_hash if known_hash is not None else get_file_hash(filepath)

        with self._lock:
            cursor = self._conn.execute("SELECT file_hash FROM files WHERE file_path = ?", (filepath,))
            row = cursor.fetchone()

            if row and row[0] == file_hash:
                # Cache hit!
                cursor = self._conn.execute(
                    "SELECT id, file_path, name, code, lineno FROM symbols WHERE file_path = ?",
                    (filepath,)
                )
                symbols = {}
                for row in cursor:
                    sym_id, f_path, name, code, lineno = row
                    symbols[sym_id] = Symbol(
                        id=sym_id,
                        file=f_path,
                        name=name,
                        code=code,
                        lineno=lineno
                    )
                return symbols

        # Cache miss or hash mismatch -> parse it (outside the lock; parsing
        # can be slow and must not serialize other threads' cache hits)
        symbols = parse_fn(filepath)

        # Persist the new state
        with self._lock, self._conn:
            # DELETE CASCADE will drop all existing symbols for this file
            self._conn.execute("DELETE FROM files WHERE file_path = ?", (filepath,))

            self._conn.execute(
                "INSERT INTO files (file_path, file_hash) VALUES (?, ?)",
                (filepath, file_hash)
            )

            if symbols:
                self._conn.executemany(
                    "INSERT INTO symbols (id, file_path, name, code, lineno) VALUES (?, ?, ?, ?, ?)",
                    [(s.id, s.file, s.name, s.code, s.lineno) for s in symbols.values()]
                )

        return symbols
