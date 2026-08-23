"""
cache.py — SQLite-backed persistent caching for AST parsed symbols and the
repository call graph.

Cache path resolution (cascade, in order):
1. DIFFCONTEXT_CACHE_DIR env var (if set)
2. repo_path/.diffcontext_cache.db (current behaviour, keeps cache locality)
3. $XDG_CACHE_HOME/diffcontext/<sha256 of abs repo_path>.db
   or ~/.cache/diffcontext/<sha256>.db
4. sqlite ':memory:' (degraded — no persistence across calls, but working)

The cascade ensures DiffContext works on read-only repos (Glama sandbox,
CI checkouts, containers with read-only rootfs, repos the user doesn't own).
Do NOT fail the call because of cache — a stale or missing cache only costs
a re-parse, never correctness.
"""

import hashlib
import json
import logging
import os
import sqlite3
from typing import Dict, Callable, List, Optional, Tuple

from .models import Symbol

logger = logging.getLogger(__name__)


def get_file_hash(filepath: str) -> str:
    """Compute SHA-256 of a file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        hasher.update(f.read())
    return hasher.hexdigest()


def hash_source(source_bytes: bytes) -> str:
    """SHA-256 of already-read file contents (avoids a second disk read)."""
    return hashlib.sha256(source_bytes).hexdigest()


def repo_state_hash(file_hashes: Dict[str, str]) -> str:
    """Single hash summarizing the content state of every Python file."""
    hasher = hashlib.sha256()
    for path in sorted(file_hashes):
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(file_hashes[path].encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _resolve_cache_path(
    repo_path: Optional[str] = None,
    cache_dir: Optional[str] = None,
) -> str:
    """
    Resolve the SQLite cache database path via a cascade.

    1. If `cache_dir` is explicitly passed (from --cache-dir CLI flag), use it.
    2. If DIFFCONTEXT_CACHE_DIR env var is set, use it.
    3. Try repo_path/.diffcontext_cache.db (keeps cache locality with the repo).
    4. Fall back to $XDG_CACHE_HOME/diffcontext/<hash>.db or ~/.cache/diffcontext/<hash>.db
    5. Fall back to ':memory:' (degraded — no persistence, but working).

    Steps 3 and 4 try to create the file/dir; if they fail (read-only
    filesystem, permission denied), the cascade continues. Step 5 always
    works.

    Returns a path string (or ':memory:').
    """
    # 1. Explicit override (highest priority)
    if cache_dir:
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            pass
        return os.path.join(cache_dir, ".diffcontext_cache.db")

    # 2. Env var override
    env_cache_dir = os.environ.get("DIFFCONTEXT_CACHE_DIR")
    if env_cache_dir:
        try:
            os.makedirs(env_cache_dir, exist_ok=True)
        except OSError:
            pass
        return os.path.join(env_cache_dir, ".diffcontext_cache.db")

    # 3. In-repo path (current behaviour, keeps cache locality)
    if repo_path:
        in_repo = os.path.join(repo_path, ".diffcontext_cache.db")
        # Try to create/test writability. If it works, use it.
        try:
            # Touch to test writability (file may already exist)
            fd = os.open(in_repo, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
            os.close(fd)
            return in_repo
        except OSError:
            logger.debug("cache: repo_path not writable (%s), falling back", in_repo)

    # 4. XDG cache dir
    xdg_cache = os.environ.get("XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache"))
    # Hash the abs repo path so different repos don't collide
    repo_hash = hashlib.sha256(os.path.abspath(repo_path or "").encode()).hexdigest()[:16]
    cache_subdir = os.path.join(xdg_cache, "diffcontext")
    xdg_path = os.path.join(cache_subdir, f"{repo_hash}.db")
    try:
        os.makedirs(cache_subdir, exist_ok=True)
        fd = os.open(xdg_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        os.close(fd)
        logger.debug("cache: using XDG cache at %s", xdg_path)
        return xdg_path
    except OSError:
        logger.debug("cache: XDG cache dir not writable (%s), falling back to :memory:", xdg_path)

    # 5. In-memory (always works, but no persistence)
    logger.debug("cache: all disk locations failed, using :memory:")
    return ":memory:"


class SymbolCache:
    """
    Persistent SQLite cache for parsed AST symbols and the call graph.

    The cache path is resolved via _resolve_cache_path(). On read-only
    repos, the cascade falls back to XDG cache or :memory: — the call
    never fails because of cache.
    """

    def __init__(self, db_path: str = ":memory:"):
        import threading
        self.db_path = db_path
        self._conn = None
        self._lock = threading.RLock()
        self._connect()

    def _connect(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
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
                # REPLACE, not plain INSERT: rows are keyed by symbols.id but
                # cleared via ON DELETE CASCADE from files.file_path, and those
                # two only line up while Symbol.file is byte-identical to the
                # filepath we just parsed. That holds for the Python parser
                # (both absolute) but is not guaranteed for a language adapter
                # that reports relative paths — stale rows would then survive
                # the DELETE above and collide here on re-index.
                self._conn.executemany(
                    "INSERT OR REPLACE INTO symbols (id, file_path, name, code, lineno) VALUES (?, ?, ?, ?, ?)",
                    [(s.id, s.file, s.name, s.code, s.lineno) for s in symbols.values()]
                )

        return symbols
