"""
cache.py — SQLite-backed persistent caching for AST parsed symbols.
"""

import hashlib
import sqlite3
import os
from contextlib import contextmanager
from typing import Dict, Callable

from .models import Symbol


def get_file_hash(filepath: str) -> str:
    """Compute SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        # Python files are small enough to read into memory safely
        hasher.update(f.read())
    return hasher.hexdigest()


class SymbolCache:
    """Persistent SQLite cache for parsed AST symbols."""

    def __init__(self, db_path: str = ".diffcontext_cache.db"):
        self.db_path = db_path
        self._conn = None
        self._connect()

    def _connect(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_db()
            
    def close(self):
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
            ''')

    def get_or_parse(self, filepath: str, parse_fn: Callable[[str], Dict[str, Symbol]]) -> Dict[str, Symbol]:
        """
        Return cached symbols if file hash matches, otherwise parse and persist.
        """
        file_hash = get_file_hash(filepath)
        
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
            
        # Cache miss or hash mismatch -> parse it
        symbols = parse_fn(filepath)
        
        # Persist the new state
        with self._conn:
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
