"""
history.py — git co-change history as a retrieval signal.

The failure taxonomy (benchmarks/EVAL_V2_REPORT.md) shows a hard ceiling
for every static signal: cross-subsystem conceptual links (a settings
flag and the security check that reads it) have no call edge, no lexical
overlap, and no co-location — graph, BM25, and hybrid all scored 0/20 on
that bucket. The only signal that CAN see those pairs is the repository's
own history: files that changed together before tend to change together
again (Zimmermann et al.'s classic co-change result, here as a retrieval
signal rather than a recommender).

Design constraints honored:
  * Zero runtime dependencies — plain `git log` via subprocess.
  * Graceful degradation — no git repo / no git binary / timeout produce
    an EMPTY index (scores_for_files returns {}), never an exception.
  * File-level granularity — symbol-level history is noisy and expensive
    to mine (rename/move tracking); file-level association is the
    literature-standard compromise. The blend spreads a file's score to
    the symbols inside it.
  * Leakage control for evaluation — `exclude_commits` lets a benchmark
    mine history WITHOUT the commits it is evaluating on. Scoring a
    commit's co-change partners with an index that already contains that
    very commit would be train-on-test leakage; the eval harness passes
    every mined eval commit here.

Association score: for a changed file c and candidate file f,
    assoc(f | c) = cochange_count(f, c) / change_count(c)
i.e. the empirical probability that a commit touching c also touched f,
maxed over all changed files. Pairs seen fewer than `min_cochanges`
times are ignored (a single shared commit is usually coincidence).
Sweeping commits (> max_files_per_commit files) are skipped entirely —
mass renames and formatting passes assert nothing about relatedness.
"""

import logging
import os
import subprocess
from collections import Counter
from typing import Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_MAX_COMMITS = 3000      # how much history to mine
MAX_FILES_PER_COMMIT = 25       # skip sweeping commits (mechanical churn)
DEFAULT_MIN_COCHANGES = 2       # pairs must co-occur at least this often


class CoChangeIndex:
    """
    File-level co-change statistics mined from `git log`.

    Usage:
        cci = CoChangeIndex("/path/to/repo")
        scores = cci.scores_for_files(["./src/auth.py"])
        # {"./src/tokens.py": 0.42, ...}  association in [0, 1]
    """

    def __init__(
        self,
        repo_path: str,
        max_commits: int = DEFAULT_MAX_COMMITS,
        max_files_per_commit: int = MAX_FILES_PER_COMMIT,
        min_cochanges: int = DEFAULT_MIN_COCHANGES,
        exclude_commits: Optional[Set[str]] = None,
    ):
        self.repo_path = os.path.abspath(repo_path)
        self.min_cochanges = min_cochanges
        self.pair_counts: Dict[str, Counter] = {}   # "./a.py" -> {"./b.py": n}
        self.file_counts: Counter = Counter()       # "./a.py" -> n commits touching it
        self.mined_commits = 0

        # Excluded hashes matched on their first 10 chars so both full and
        # abbreviated hashes (as eval harnesses store them) work.
        excluded = {h[:10] for h in (exclude_commits or set())}

        commits = self._read_history(max_commits)
        for commit_hash, files in commits:
            if commit_hash[:10] in excluded:
                continue
            files = [f for f in files if f.endswith(".py")]
            if len(files) < 2 or len(files) > max_files_per_commit:
                # <2: nothing co-changed; >cap: sweeping mechanical commit
                if files:
                    self.mined_commits += 1
                    for f in files:
                        self.file_counts["./" + f] += 1
                continue
            self.mined_commits += 1
            rels = ["./" + f for f in files]
            for f in rels:
                self.file_counts[f] += 1
            for i, a in enumerate(rels):
                counter_a = self.pair_counts.setdefault(a, Counter())
                for b in rels[i + 1:]:
                    counter_a[b] += 1
                    self.pair_counts.setdefault(b, Counter())[a] += 1

    def _read_history(self, max_commits: int):
        """[(full_hash, [file, ...]), ...] — empty list on any failure."""
        try:
            res = subprocess.run(
                ["git", "log", f"--max-count={max_commits}", "--no-merges",
                 "--name-only", "--format=%x00%H"],
                cwd=self.repo_path, capture_output=True, text=True, timeout=120,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning("co-change mining skipped: git log failed (%s)", e)
            return []
        if res.returncode != 0:
            logger.warning(
                "co-change mining skipped: git log exited %d", res.returncode
            )
            return []

        commits = []
        for block in res.stdout.split("\x00"):
            if not block.strip():
                continue
            lines = block.strip().split("\n")
            commit_hash = lines[0].strip()
            files = [ln.strip() for ln in lines[1:] if ln.strip()]
            commits.append((commit_hash, files))
        return commits

    def scores_for_files(self, changed_files: Iterable[str]) -> Dict[str, float]:
        """
        Association score in [0, 1] for every file that historically
        co-changed with any of `changed_files` (max over the changed
        files). Accepts "./rel/path.py" or "rel/path.py". The changed
        files themselves are excluded from the result.
        """
        changed = {
            f if f.startswith("./") else "./" + f for f in changed_files
        }
        out: Dict[str, float] = {}
        for c in changed:
            total = self.file_counts.get(c, 0)
            if total <= 0:
                continue
            for f, n in self.pair_counts.get(c, Counter()).items():
                if f in changed or n < self.min_cochanges:
                    continue
                assoc = n / total
                if assoc > out.get(f, 0.0):
                    out[f] = assoc
        return out

    def scores_for_symbols(self, changed_symbols: Iterable[str]) -> Dict[str, float]:
        """Convenience: same as scores_for_files, keyed off the file part
        of changed symbol IDs ("./a.py:fn" -> "./a.py")."""
        return self.scores_for_files(
            {s.split(":")[0] for s in changed_symbols}
        )
