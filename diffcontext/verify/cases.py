"""
cases.py — User-defined test cases for context retrieval, and calibration.

A test case states an expectation the user KNOWS to be true about their
own repo: "when function X changes, a correct context must include Y".
Running the cases measures recall against those expectations; running
them with --calibrate additionally checks whether the structural
sufficiency score (sufficiency.py) actually tracks measured recall —
which is what turns the score from a heuristic into calibrated confidence.

Case file format (JSON; YAML also accepted if PyYAML is installed):

    {
      "version": 1,
      "defaults": {"budget": 10000, "depth": 2, "top_k": 20, "min_recall": 1.0},
      "cases": [
        {
          "name": "jwt-validation-change",
          "task": "optional: what the change/request is about, in plain English",
          "changed": ["./auth.py:validate_jwt"],
          "must_include": ["./api.py:get_user", "./middleware.py:check_auth"],
          "must_exclude": ["./billing.py:invoice_total"],
          "budget": 8000,
          "min_recall": 1.0
        }
      ]
    }

Field semantics:
  changed       (required) symbol IDs treated as the modified code.
  must_include  (required) symbols a sufficient context MUST contain.
  must_exclude  (optional) symbols that must NOT appear (precision guard).
  task          (optional) natural-language intent; recorded in results,
                reserved for future query-aware ranking.
  budget        token budget for compilation (0 = unlimited).
  top_k         max context symbols per changed symbol (0 = unlimited).
  depth         max dependency traversal depth.
  min_recall    pass threshold on must_include recall (default 1.0).

Pass rule: recall >= min_recall AND no must_exclude symbol was selected.
Symbols that don't exist in the index count as failures but are flagged
loudly with fuzzy-match suggestions, so a typo can't silently pass or
quietly deflate your numbers.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..pipeline import index_repository, analyze_impact, compile as compile_pipeline
from ..models import RepositoryIndex
from .sufficiency import analyze_sufficiency, SufficiencyReport
from .history import extract_cochange_cases

DEFAULT_BUDGET = 10000
DEFAULT_DEPTH = 2
DEFAULT_TOP_K = 20        # per changed symbol; benchmarked sweet spot
DEFAULT_MIN_RECALL = 1.0


@dataclass
class Case:
    """One user-defined retrieval expectation."""
    name: str
    changed: List[str]
    must_include: List[str]
    must_exclude: List[str] = field(default_factory=list)
    task: str = ""
    budget: int = DEFAULT_BUDGET
    depth: int = DEFAULT_DEPTH
    top_k: int = DEFAULT_TOP_K
    min_recall: float = DEFAULT_MIN_RECALL

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "changed": self.changed,
            "must_include": self.must_include,
        }
        if self.must_exclude:
            d["must_exclude"] = self.must_exclude
        if self.task:
            d["task"] = self.task
        if self.budget != DEFAULT_BUDGET:
            d["budget"] = self.budget
        if self.depth != DEFAULT_DEPTH:
            d["depth"] = self.depth
        if self.top_k != DEFAULT_TOP_K:
            d["top_k"] = self.top_k
        if self.min_recall != DEFAULT_MIN_RECALL:
            d["min_recall"] = self.min_recall
        return d


@dataclass
class CaseResult:
    """Outcome of running one case against the pipeline."""
    case: Case
    passed: bool
    recall: float                      # |must_include ∩ selected| / |must_include|
    missing: List[str]                 # must_include symbols not selected
    forbidden_hits: List[str]          # must_exclude symbols that WERE selected
    unknown_symbols: Dict[str, str]    # symbol -> suggestion ("" if none)
    selected_count: int
    context_tokens: int
    sufficiency: Optional[SufficiencyReport] = None
    # |must_include ∩ retrieved| / |retrieved non-changed symbols|. A LOWER
    # BOUND on true precision: co-change ground truth is incomplete, so some
    # "noise" symbols are actually relevant (measured small — GT-adjusted
    # precision stays within ~2x; RIGOR_REPORT_2026-07.md §2). None when
    # nothing beyond the changed symbols was selected.
    precision_lb: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "name": self.case.name,
            "passed": self.passed,
            "recall": round(self.recall, 3),
            "precision_lb": (
                round(self.precision_lb, 3) if self.precision_lb is not None else None
            ),
            "missing": self.missing,
            "forbidden_hits": self.forbidden_hits,
            "unknown_symbols": self.unknown_symbols,
            "selected_count": self.selected_count,
            "context_tokens": self.context_tokens,
            "sufficiency_score": (
                round(self.sufficiency.score, 1) if self.sufficiency else None
            ),
            "sufficiency_verdict": (
                self.sufficiency.verdict if self.sufficiency else None
            ),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

class CaseFormatError(ValueError):
    """Raised when a case file is malformed, with a message that says how to fix it."""


def load_cases(path: str) -> List[Case]:
    """
    Load cases from a JSON (or YAML, if PyYAML is installed) file.

    Raises CaseFormatError with an actionable message on any structural
    problem — a silent skip here would corrupt every number downstream.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw_text = f.read()

    data = None
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # optional dependency
        except ImportError:
            raise CaseFormatError(
                f"{path} is YAML but PyYAML is not installed. "
                "Either `pip install pyyaml` or convert the file to JSON."
            )
        data = yaml.safe_load(raw_text)
    else:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise CaseFormatError(f"{path} is not valid JSON: {e}")

    if not isinstance(data, dict) or "cases" not in data:
        raise CaseFormatError(
            f'{path} must be an object with a "cases" list '
            '(see docs/VERIFY.md for the format).'
        )

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise CaseFormatError('"defaults" must be an object.')

    cases: List[Case] = []
    for i, entry in enumerate(data["cases"]):
        if not isinstance(entry, dict):
            raise CaseFormatError(f"cases[{i}] must be an object.")
        for req in ("changed", "must_include"):
            if req not in entry or not isinstance(entry[req], list) or not entry[req]:
                raise CaseFormatError(
                    f'cases[{i}] ("{entry.get("name", "?")}") needs a non-empty '
                    f'"{req}" list of symbol IDs like "./auth.py:validate_jwt".'
                )
        cases.append(Case(
            name=entry.get("name", f"case-{i}"),
            changed=list(entry["changed"]),
            must_include=list(entry["must_include"]),
            must_exclude=list(entry.get("must_exclude", [])),
            task=entry.get("task", ""),
            budget=int(entry.get("budget", defaults.get("budget", DEFAULT_BUDGET))),
            depth=int(entry.get("depth", defaults.get("depth", DEFAULT_DEPTH))),
            top_k=int(entry.get("top_k", defaults.get("top_k", DEFAULT_TOP_K))),
            min_recall=float(
                entry.get("min_recall", defaults.get("min_recall", DEFAULT_MIN_RECALL))
            ),
        ))

    if not cases:
        raise CaseFormatError(f'{path} has an empty "cases" list.')
    return cases


def save_cases(cases: List[Case], path: str) -> None:
    """Write cases to a JSON file in the documented format."""
    payload = {"version": 1, "cases": [c.to_dict() for c in cases]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# History-derived cases
# ---------------------------------------------------------------------------

def cases_from_history(repo_path: str, max_cases: int = 30) -> List[Case]:
    """
    Auto-generate cases from git co-change history: functions modified in
    the same commit are external evidence of relatedness (human behavior,
    not our graph). One case per query symbol.
    """
    cochange = extract_cochange_cases(repo_path, max_cases=max_cases)
    cases = []
    for cc in cochange:
        cases.append(Case(
            name=f"history-{cc.commit_hash}-{cc.query_symbol.split(':')[-1]}",
            task=f"co-change from commit {cc.commit_hash}: {cc.commit_msg}",
            changed=[cc.query_symbol],
            must_include=list(cc.ground_truth_symbols),
            # History cases are noisy (a commit can touch unrelated code),
            # so demand majority recall rather than perfection.
            min_recall=0.5,
        ))
    return cases


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _suggest(unknown: str, known) -> str:
    # Shared fast path — see _suggest_similar_symbol for why plain
    # get_close_matches chokes on symbol IDs (long shared path prefixes
    # defeat difflib's prefilters).
    from ..pipeline import _suggest_similar_symbol
    return _suggest_similar_symbol(unknown, known) or ""


def run_cases(
    repo_path: str,
    cases: List[Case],
    index: Optional[RepositoryIndex] = None,
    cutoff: Optional[str] = None,
) -> List[CaseResult]:
    """
    Run every case against the real pipeline (index once, reuse).

    Recall counts ALL must_include entries in the denominator — a symbol
    that doesn't exist in the index is a miss, not a silent skip, and gets
    flagged with a fuzzy suggestion so typos are visible in the report.

    cutoff: selection policy forwarded to compile ("gap" = the measured
    precision operating point) so users can measure the recall/precision
    tradeoff on their own repo's cases before adopting it.
    """
    repo_path = os.path.abspath(repo_path)
    idx = index or index_repository(repo_path)
    known_ids = idx.symbols.keys()

    results: List[CaseResult] = []
    for case in cases:
        unknown: Dict[str, str] = {}
        for sym in case.changed + case.must_include + case.must_exclude:
            if sym not in idx.symbols and sym not in idx.graph:
                unknown[sym] = _suggest(sym, known_ids)

        impact = analyze_impact(idx, case.changed, max_depth=case.depth)
        max_tokens = case.budget if case.budget > 0 else None
        top_k = case.top_k * len(case.changed) if case.top_k > 0 else None
        package = compile_pipeline(
            idx, impact, max_tokens=max_tokens, top_k=top_k, cutoff=cutoff,
        )

        selected = {item.symbol_id for item in package.items}
        want = set(case.must_include)
        hit = want & selected
        recall = len(hit) / len(want)
        missing = sorted(want - selected)
        forbidden_hits = sorted(set(case.must_exclude) & selected)

        # Precision over what was actually retrieved (changed symbols are
        # the query, not retrieval). Lower bound — see CaseResult.
        retrieved = selected - set(case.changed)
        precision_lb = (
            len(want & retrieved) / len(retrieved) if retrieved else None
        )

        passed = recall >= case.min_recall and not forbidden_hits

        sufficiency = analyze_sufficiency(idx, impact, package)

        results.append(CaseResult(
            case=case,
            passed=passed,
            recall=recall,
            missing=missing,
            forbidden_hits=forbidden_hits,
            unknown_symbols=unknown,
            selected_count=len(selected),
            context_tokens=package.token_estimate,
            sufficiency=sufficiency,
            precision_lb=precision_lb,
        ))
    return results


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

@dataclass
class CalibrationBucket:
    lo: float
    hi: float
    n: int
    mean_recall: float


@dataclass
class Calibration:
    """Does the structural sufficiency score track measured recall?"""
    buckets: List[CalibrationBucket]
    pearson_r: Optional[float]     # None when undefined (constant series)
    n_cases: int
    # Fitted per-repo recall predictor over runtime-available features
    # (see MODEL_FEATURES). None when too few cases to fit responsibly.
    # Measured basis: benchmarks/calibration_at_scale.py — re-weighting the
    # four score components alone has ~zero held-out predictive power, but
    # this extended feature set beat the predict-the-mean baseline on
    # held-out MAE in 8/9 Python repos (held-out r up to 0.65).
    model: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "n_cases": self.n_cases,
            "pearson_r": round(self.pearson_r, 3) if self.pearson_r is not None else None,
            "buckets": [
                {"range": [b.lo, b.hi], "n": b.n, "mean_recall": round(b.mean_recall, 3)}
                for b in self.buckets
            ],
            "model": self.model,
        }


# Runtime-available features for the fitted recall predictor. Every one is
# computable BEFORE knowing the answer: score components plus how much the
# selector kept/cut. (Ground-truth-dependent quantities must never be here.)
MODEL_FEATURES = (
    "direct_closure", "high_score_retention", "local_graph_confidence",
    "parse_health", "selected_count", "n_missing_direct", "n_dropped_high",
    "context_tokens",
)
MIN_MODEL_CASES = 30
CALIBRATION_FILENAME = ".diffcontext-calibration.json"


def _model_features(sufficiency: SufficiencyReport, selected_count: int,
                    context_tokens: int) -> List[float]:
    return [
        sufficiency.direct_closure,
        sufficiency.high_score_retention,
        sufficiency.local_graph_confidence,
        sufficiency.parse_health,
        float(selected_count),
        float(len(sufficiency.missing_direct)),
        float(len(sufficiency.dropped_high_score)),
        float(context_tokens),
    ]


def _solve_linear(A: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """Gaussian elimination with partial pivoting. Returns None if singular."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None
        M[col], M[piv] = M[piv], M[col]
        for r in range(col + 1, n):
            f = M[r][col] / M[col][col]
            for c in range(col, n + 1):
                M[r][c] -= f * M[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][c] * x[c] for c in range(r + 1, n))) / M[r][r]
    return x


def fit_recall_model(results: List[CaseResult]) -> Optional[dict]:
    """
    Least-squares fit of measured recall on standardized runtime features.
    Dependency-free (pure Python normal equations). Returns None when there
    are too few cases — a model fit on a handful of points is noise with a
    JSON file, and we refuse to produce one.
    """
    rows = [r for r in results if r.sufficiency is not None]
    if len(rows) < MIN_MODEL_CASES:
        return None
    X = [_model_features(r.sufficiency, r.selected_count, r.context_tokens)
         for r in rows]
    y = [r.recall for r in rows]
    n, d = len(X), len(MODEL_FEATURES)

    means = [sum(row[j] for row in X) / n for j in range(d)]
    stds = []
    for j in range(d):
        var = sum((row[j] - means[j]) ** 2 for row in X) / n
        stds.append(var ** 0.5 if var > 1e-12 else 1.0)
    Z = [[(row[j] - means[j]) / stds[j] for j in range(d)] + [1.0] for row in X]

    # Ridge-regularized normal equations: (Z^T Z + λI) w = Z^T y. The tiny
    # λ exists for degenerate columns — a zero-variance feature (e.g.
    # parse_health on a repo with no broken files) standardizes to an
    # all-zero column and would make plain least squares singular; with
    # ridge it just gets weight 0. The intercept is not penalized.
    dim = d + 1
    lam = 1e-6
    ZtZ = [[sum(Z[i][a] * Z[i][b_] for i in range(n))
            + (lam if (a == b_ and a < d) else 0.0)
            for b_ in range(dim)]
           for a in range(dim)]
    Zty = [sum(Z[i][a] * y[i] for i in range(n)) for a in range(dim)]
    w = _solve_linear(ZtZ, Zty)
    if w is None:
        return None

    preds = [max(0.0, min(1.0, sum(Z[i][a] * w[a] for a in range(dim))))
             for i in range(n)]
    mean_y = sum(y) / n
    mae = sum(abs(p - yy) for p, yy in zip(preds, y)) / n
    baseline_mae = sum(abs(mean_y - yy) for yy in y) / n
    return {
        "version": 1,
        "features": list(MODEL_FEATURES),
        "means": [round(v, 6) for v in means],
        "stds": [round(v, 6) for v in stds],
        "weights": [round(v, 6) for v in w[:-1]],
        "intercept": round(w[-1], 6),
        "n_cases": n,
        "mean_recall": round(mean_y, 4),
        "train_mae": round(mae, 4),
        "baseline_mae": round(baseline_mae, 4),
    }


def predict_recall(model: dict, sufficiency: SufficiencyReport,
                   selected_count: int, context_tokens: int) -> float:
    """Apply a fitted calibration model; returns predicted recall in [0,1]."""
    feats = _model_features(sufficiency, selected_count, context_tokens)
    z = [(feats[j] - model["means"][j]) / model["stds"][j]
         for j in range(len(model["features"]))]
    raw = sum(zj * wj for zj, wj in zip(z, model["weights"])) + model["intercept"]
    return max(0.0, min(1.0, raw))


def save_calibration(cal: Calibration, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cal.to_dict(), f, indent=2)
        f.write("\n")


def load_calibration(path: str) -> Optional[dict]:
    """Load a saved calibration file; returns its dict or None if absent/bad."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def calibrate(results: List[CaseResult]) -> Calibration:
    """
    Map sufficiency-score buckets to observed recall, plus a Pearson
    correlation. A positive, monotonic relationship is the evidence that
    the structural score means something on this repo; a flat or negative
    one is an honest null result and should be reported as such.
    """
    pairs = [
        (r.sufficiency.score, r.recall)
        for r in results if r.sufficiency is not None
    ]
    n = len(pairs)

    buckets: List[CalibrationBucket] = []
    for lo in (0, 20, 40, 60, 80):
        hi = lo + 20
        in_bucket = [rec for s, rec in pairs if lo <= s < hi or (hi == 100 and s == 100)]
        buckets.append(CalibrationBucket(
            lo=lo, hi=hi, n=len(in_bucket),
            mean_recall=(sum(in_bucket) / len(in_bucket)) if in_bucket else 0.0,
        ))

    pearson: Optional[float] = None
    if n >= 3:
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        mx = sum(xs) / n
        my = sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in pairs)
        vx = sum((x - mx) ** 2 for x in xs)
        vy = sum((y - my) ** 2 for y in ys)
        if vx > 0 and vy > 0:
            pearson = cov / (vx ** 0.5 * vy ** 0.5)

    return Calibration(buckets=buckets, pearson_r=pearson, n_cases=n,
                       model=fit_recall_model(results))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_results(results: List[CaseResult]) -> str:
    lines = ["=== DIFFCONTEXT VERIFY: CASE RESULTS ==="]
    n_pass = sum(1 for r in results if r.passed)
    for r in results:
        mark = "✓" if r.passed else "✗"
        suff = f"suff={r.sufficiency.score:.0f}" if r.sufficiency else "suff=?"
        lines.append(
            f"  {mark} {r.case.name}: recall {r.recall * 100:.0f}% "
            f"(need ≥{r.case.min_recall * 100:.0f}%), "
            f"{r.selected_count} symbols, {suff}"
        )
        for m in r.missing[:5]:
            lines.append(f"      missing: {m}")
        if len(r.missing) > 5:
            lines.append(f"      ... and {len(r.missing) - 5} more missing")
        for fh in r.forbidden_hits:
            lines.append(f"      FORBIDDEN symbol selected: {fh}")
        for sym, sugg in r.unknown_symbols.items():
            hint = f" — did you mean '{sugg}'?" if sugg else ""
            lines.append(f"      ⚠ '{sym}' not found in index (typo?){hint}")
    mean_recall = sum(r.recall for r in results) / len(results) if results else 0.0
    lines.append("")
    total = (
        f"TOTAL: {n_pass}/{len(results)} passed, mean recall {mean_recall * 100:.1f}%"
    )
    with_prec = [r for r in results if r.precision_lb is not None]
    if with_prec:
        mean_prec = sum(r.precision_lb for r in with_prec) / len(with_prec)
        mean_syms = sum(r.selected_count for r in with_prec) / len(with_prec)
        total += (
            f", mean precision ≥{mean_prec * 100:.1f}% "
            f"({mean_syms:.1f} symbols/case; lower bound — co-change GT is "
            f"incomplete)"
        )
    lines.append(total)
    lines.append("=== END CASE RESULTS ===")
    return "\n".join(lines)


def render_calibration(cal: Calibration) -> str:
    lines = [
        "=== CALIBRATION: structural score vs measured recall ===",
        f"Cases: {cal.n_cases}",
    ]
    for b in cal.buckets:
        bar = "#" * int(b.mean_recall * 20)
        lines.append(
            f"  score {b.lo:>3.0f}-{b.hi:<3.0f}: n={b.n:<3d} "
            f"mean recall {b.mean_recall * 100:5.1f}%  {bar}"
        )
    if cal.pearson_r is not None:
        lines.append(f"Pearson r (score vs recall): {cal.pearson_r:+.3f}")
        if cal.pearson_r >= 0.4:
            lines.append(
                "→ The structural score tracks measured recall on this repo: "
                "higher scores are earned, not decorative."
            )
        elif cal.pearson_r >= 0.1:
            lines.append(
                "→ Weak positive relationship. Treat the score as a coarse "
                "warning signal, not confidence."
            )
        else:
            lines.append(
                "→ NULL RESULT: the structural score does NOT track recall on "
                "this repo. Do not trust the score here; trust the per-case "
                "findings instead. (Reporting this honestly is the point.)"
            )
    else:
        lines.append("Pearson r: undefined (need ≥3 cases with score/recall variance)")
    if cal.model is not None:
        m = cal.model
        lines.append("")
        lines.append(
            f"Fitted recall predictor ({m['n_cases']} cases): train MAE "
            f"{m['train_mae']:.3f} vs predict-the-mean {m['baseline_mae']:.3f}."
        )
        lines.append(
            "Save it with --save-calibration; `diffcontext verify` will then "
            "report a calibrated recall estimate instead of a bare score."
        )
    elif cal.n_cases < MIN_MODEL_CASES:
        lines.append("")
        lines.append(
            f"(No recall predictor fitted: {cal.n_cases} cases < "
            f"{MIN_MODEL_CASES} minimum. Run with more history cases.)"
        )
    lines.append("=== END CALIBRATION ===")
    return "\n".join(lines)
