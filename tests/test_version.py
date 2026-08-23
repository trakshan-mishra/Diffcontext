"""
tests/test_version.py — Assert __version__ is valid semver and matches the
newest CHANGELOG heading. Prevents the stale-version drift that left
__version__ at 0.3.0 while GitHub releases went to 0.4.1.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext import __version__


CHANGELOG = os.path.join(
    os.path.dirname(__file__), "..", "CHANGELOG.md",
)


def _newest_changelog_version():
    """Return the first ## heading in CHANGELOG.md that looks like a version."""
    with open(CHANGELOG) as f:
        for line in f:
            m = re.match(r"^## \[(\d+\.\d+\.\d+)\]", line)
            if m:
                return m.group(1)
    return None


class TestVersionCoherence:
    def test_version_is_semver(self):
        m = re.match(r"^\d+\.\d+\.\d+$", __version__)
        assert m is not None, f"__version__={__version__!r} is not semver"

    def test_matches_changelog(self):
        newest = _newest_changelog_version()
        assert newest is not None, "no version heading in CHANGELOG.md"
        assert __version__ == newest, (
            f"__version__={__version__!r} != newest CHANGELOG heading {newest!r}"
        )
