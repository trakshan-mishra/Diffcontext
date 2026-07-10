"""
tests/test_cache.py — SymbolCache behavior: hit, miss, invalidation, cascade.

Uses real files and a real sqlite db under tmp_path; parse_fn call counts
prove whether the cache actually short-circuited parsing.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.cache import SymbolCache, get_file_hash
from diffcontext.models import Symbol


def _parse_counting(counter):
    """A parse_fn that records how many times it ran and returns one symbol
    per `def` line in the file (enough realism for cache semantics)."""
    def parse(filepath):
        counter.append(filepath)
        symbols = {}
        with open(filepath) as f:
            for lineno, line in enumerate(f, 1):
                if line.startswith("def "):
                    name = line.split("(")[0][4:]
                    sym_id = f"./{os.path.basename(filepath)}:{name}"
                    symbols[sym_id] = Symbol(
                        id=sym_id, file=filepath, name=name,
                        code=line.rstrip(), lineno=lineno,
                    )
        return symbols
    return parse


class TestGetFileHash:
    def test_stable_and_content_sensitive(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("def a():\n    pass\n")
        h1 = get_file_hash(str(f))
        assert h1 == get_file_hash(str(f))          # deterministic
        f.write_text("def a():\n    return 2\n")
        assert get_file_hash(str(f)) != h1          # content-sensitive


class TestSymbolCache:
    def test_miss_then_hit(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def alpha():\n    pass\ndef beta():\n    pass\n")
        calls = []
        with SymbolCache(str(tmp_path / "cache.db")) as cache:
            first = cache.get_or_parse(str(f), _parse_counting(calls))
            assert len(calls) == 1
            assert set(first) == {"./mod.py:alpha", "./mod.py:beta"}

            second = cache.get_or_parse(str(f), _parse_counting(calls))
            assert len(calls) == 1                   # served from cache
            assert set(second) == set(first)
            got = second["./mod.py:alpha"]
            assert (got.name, got.lineno) == ("alpha", 1)

    def test_invalidation_on_content_change(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def alpha():\n    pass\n")
        calls = []
        with SymbolCache(str(tmp_path / "cache.db")) as cache:
            cache.get_or_parse(str(f), _parse_counting(calls))
            f.write_text("def alpha():\n    pass\ndef gamma():\n    pass\n")
            symbols = cache.get_or_parse(str(f), _parse_counting(calls))
            assert len(calls) == 2                   # hash mismatch → re-parse
            assert "./mod.py:gamma" in symbols

    def test_removed_symbols_do_not_linger(self, tmp_path):
        # DELETE ... CASCADE must drop the old file's symbols; a symbol
        # deleted from the source file must not survive in the cache.
        f = tmp_path / "mod.py"
        f.write_text("def alpha():\n    pass\ndef beta():\n    pass\n")
        calls = []
        with SymbolCache(str(tmp_path / "cache.db")) as cache:
            cache.get_or_parse(str(f), _parse_counting(calls))
            f.write_text("def alpha():\n    pass\n")   # beta deleted
            symbols = cache.get_or_parse(str(f), _parse_counting(calls))
            assert "./mod.py:beta" not in symbols

            # And the hit path (a third call, no change) agrees:
            symbols = cache.get_or_parse(str(f), _parse_counting(calls))
            assert len(calls) == 2
            assert "./mod.py:beta" not in symbols

    def test_persistence_across_connections(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def alpha():\n    pass\n")
        db = str(tmp_path / "cache.db")
        calls = []
        with SymbolCache(db) as cache:
            cache.get_or_parse(str(f), _parse_counting(calls))
        # New process simulated by a fresh connection to the same db file
        with SymbolCache(db) as cache2:
            symbols = cache2.get_or_parse(str(f), _parse_counting(calls))
            assert len(calls) == 1                   # still cached on disk
            assert "./mod.py:alpha" in symbols
