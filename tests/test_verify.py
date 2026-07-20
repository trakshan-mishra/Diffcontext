"""
tests/test_verify.py — sufficiency scoring, user test cases, calibration,
and history-derived case generation (diffcontext.verify).
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.pipeline import index_repository, analyze_impact, compile as dc_compile
from diffcontext.verify import (
    Case,
    CaseFormatError,
    analyze_sufficiency,
    calibrate,
    cases_from_history,
    load_cases,
    run_cases,
    save_cases,
    render_results,
    render_calibration,
)
from diffcontext.verify.cases import CaseResult
from diffcontext.verify.sufficiency import SufficiencyReport

BASE = os.path.dirname(os.path.dirname(__file__))
MEDIUM = os.path.join(BASE, "tests", "fixtures", "medium_repo")

CHANGED = "./service.py:create_order"
DIRECT_CALLER = "./service.py:onboard_user"
DIRECT_CALLEE = "./validators.py:is_positive"


def _compile_for(changed, max_tokens=10000, top_k=None):
    idx = index_repository(MEDIUM)
    impact = analyze_impact(idx, [changed])
    pkg = dc_compile(idx, impact, max_tokens=max_tokens, top_k=top_k)
    return idx, impact, pkg


class TestSufficiency:
    def test_full_budget_is_sufficient(self):
        idx, impact, pkg = _compile_for(CHANGED)
        report = analyze_sufficiency(idx, impact, pkg)
        assert isinstance(report, SufficiencyReport)
        assert report.verdict == "SUFFICIENT"
        assert report.direct_closure == 1.0
        assert report.missing_direct == []
        assert report.score >= 80

    def test_starved_budget_reports_missing_direct_neighbors(self):
        # max_tokens=1: selector keeps only the changed symbol (per-symbol
        # cap rejects everything else), so every direct neighbor is missing.
        idx, impact, pkg = _compile_for(CHANGED, max_tokens=1)
        report = analyze_sufficiency(idx, impact, pkg)
        assert report.direct_closure < 1.0
        assert DIRECT_CALLER in report.missing_direct
        assert DIRECT_CALLEE in report.missing_direct
        assert report.verdict in ("DEGRADED", "INSUFFICIENT")
        kinds = {f.kind for f in report.findings}
        assert "missing-direct-neighbor" in kinds
        assert "high-score-dropped" in kinds

    def test_starved_scores_strictly_below_full(self):
        idx, impact, full_pkg = _compile_for(CHANGED)
        _, _, starved_pkg = _compile_for(CHANGED, max_tokens=1)
        full = analyze_sufficiency(idx, impact, full_pkg)
        starved = analyze_sufficiency(idx, impact, starved_pkg)
        assert starved.score < full.score

    def test_to_dict_and_render(self):
        idx, impact, pkg = _compile_for(CHANGED, max_tokens=1)
        report = analyze_sufficiency(idx, impact, pkg)
        d = report.to_dict()
        assert set(d["components"]) == {
            "direct_closure", "high_score_retention",
            "local_graph_confidence", "parse_health",
        }
        text = report.render()
        assert report.verdict in text
        assert "structural proxy" in text  # honesty note present when uncalibrated

    def test_zero_evidence_scores_unknown_not_perfect(self):
        # A symbol with no edges, no neighbors, and no ranked-relevant
        # candidates gives the score NOTHING to observe. The legacy formula
        # scored this as a perfect 100 (the TypeScript constant-100 bug);
        # it must now sit at the "don't know" midpoint with a low-evidence
        # finding, not feign confidence.
        from diffcontext.models import (
            RepositoryIndex, ImpactResult, ContextPackage, Symbol,
        )
        q = "./lonely.py:orphan"
        idx = RepositoryIndex(
            symbols={q: Symbol(id=q, file="lonely.py", name="orphan",
                               code="def orphan(): pass")},
            graph={q: []},
        )
        impact = ImpactResult(changed=[q], scores={})
        pkg = ContextPackage(text="", symbol_count=1, token_estimate=10,
                             total_repo_tokens=10)
        report = analyze_sufficiency(idx, impact, pkg)
        assert report.evidence < 0.2
        assert abs(report.score - 50.0) < 10.0
        assert report.score_legacy == 100.0
        assert "low-evidence" in {f.kind for f in report.findings}

    def test_rich_evidence_matches_legacy_formula(self):
        # With a well-connected changed symbol the evidence factor saturates
        # and the new score converges to the legacy one.
        idx, impact, pkg = _compile_for(CHANGED)
        report = analyze_sufficiency(idx, impact, pkg)
        assert report.evidence > 0.7
        assert abs(report.score - report.score_legacy) < 15.0


class TestCaseLoading:
    def test_roundtrip_save_load(self, tmp_path):
        cases = [Case(
            name="c1", changed=[CHANGED], must_include=[DIRECT_CALLER],
            must_exclude=["./models.py:User.display_name"],
            task="order totals", budget=5000, min_recall=0.5,
        )]
        path = str(tmp_path / "cases.json")
        save_cases(cases, path)
        loaded = load_cases(path)
        assert len(loaded) == 1
        assert loaded[0].name == "c1"
        assert loaded[0].must_include == [DIRECT_CALLER]
        assert loaded[0].min_recall == 0.5
        assert loaded[0].budget == 5000

    def test_defaults_block_applies(self, tmp_path):
        path = str(tmp_path / "cases.json")
        with open(path, "w") as f:
            json.dump({
                "version": 1,
                "defaults": {"budget": 1234, "min_recall": 0.7},
                "cases": [{"name": "x", "changed": [CHANGED],
                           "must_include": [DIRECT_CALLER]}],
            }, f)
        (case,) = load_cases(path)
        assert case.budget == 1234
        assert case.min_recall == 0.7

    def test_missing_must_include_raises(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            json.dump({"cases": [{"name": "x", "changed": [CHANGED]}]}, f)
        with pytest.raises(CaseFormatError, match="must_include"):
            load_cases(path)

    def test_invalid_json_raises(self, tmp_path):
        path = str(tmp_path / "broken.json")
        with open(path, "w") as f:
            f.write("{not json")
        with pytest.raises(CaseFormatError, match="not valid JSON"):
            load_cases(path)

    def test_top_level_must_have_cases(self, tmp_path):
        path = str(tmp_path / "list.json")
        with open(path, "w") as f:
            json.dump([{"name": "x"}], f)
        with pytest.raises(CaseFormatError, match='"cases"'):
            load_cases(path)


class TestRunCases:
    def test_direct_neighbors_are_retrieved(self):
        case = Case(name="direct", changed=[CHANGED],
                    must_include=[DIRECT_CALLER, DIRECT_CALLEE])
        (result,) = run_cases(MEDIUM, [case])
        assert result.recall == 1.0
        assert result.passed
        assert result.missing == []
        assert result.sufficiency is not None

    def test_unknown_symbol_counts_as_miss_and_is_flagged(self):
        typo = "./service.py:create_ordr"   # missing 'e'
        case = Case(name="typo", changed=[CHANGED], must_include=[typo])
        (result,) = run_cases(MEDIUM, [case])
        assert not result.passed
        assert result.recall == 0.0
        assert typo in result.unknown_symbols
        assert result.unknown_symbols[typo] == CHANGED  # fuzzy suggestion

    def test_must_exclude_hit_fails_case(self):
        case = Case(name="excl", changed=[CHANGED],
                    must_include=[DIRECT_CALLER],
                    must_exclude=[DIRECT_CALLEE])   # will certainly be selected
        (result,) = run_cases(MEDIUM, [case])
        assert not result.passed
        assert result.forbidden_hits == [DIRECT_CALLEE]

    def test_min_recall_threshold(self):
        # One real target, one nonexistent: recall 0.5
        case = Case(name="half", changed=[CHANGED],
                    must_include=[DIRECT_CALLER, "./nowhere.py:ghost"],
                    min_recall=0.5)
        (result,) = run_cases(MEDIUM, [case])
        assert result.recall == 0.5
        assert result.passed

    def test_render_results_mentions_pass_counts(self):
        case = Case(name="direct", changed=[CHANGED],
                    must_include=[DIRECT_CALLER])
        results = run_cases(MEDIUM, [case])
        text = render_results(results)
        assert "1/1 passed" in text


class TestCalibration:
    def _fake_result(self, score, recall):
        case = Case(name=f"s{score}", changed=["x"], must_include=["y"])
        suff = SufficiencyReport(
            score=score, verdict="SUFFICIENT", direct_closure=1.0,
            high_score_retention=1.0, local_graph_confidence=1.0,
            parse_health=1.0,
        )
        return CaseResult(
            case=case, passed=True, recall=recall, missing=[],
            forbidden_hits=[], unknown_symbols={}, selected_count=1,
            context_tokens=100, sufficiency=suff,
        )

    def test_positive_correlation_detected(self):
        results = [self._fake_result(s, r) for s, r in
                   [(10, 0.1), (30, 0.3), (50, 0.5), (70, 0.7), (90, 0.9)]]
        cal = calibrate(results)
        assert cal.n_cases == 5
        assert cal.pearson_r == pytest.approx(1.0)
        # Bucket means reflect the recall values placed in each bucket
        assert cal.buckets[0].mean_recall == pytest.approx(0.1)
        assert cal.buckets[4].mean_recall == pytest.approx(0.9)
        assert "tracks measured recall" in render_calibration(cal)

    def test_constant_score_gives_undefined_pearson(self):
        results = [self._fake_result(80, r) for r in (0.2, 0.5, 0.8)]
        cal = calibrate(results)
        assert cal.pearson_r is None

    def test_null_result_reported_honestly(self):
        results = [self._fake_result(s, r) for s, r in
                   [(10, 0.9), (50, 0.5), (90, 0.1)]]
        cal = calibrate(results)
        assert cal.pearson_r < 0
        assert "NULL RESULT" in render_calibration(cal)


class TestHistoryCases:
    def _git(self, cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)

    def test_cochange_commit_becomes_case(self, tmp_path):
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        self._git(repo, "init")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "t")

        src = os.path.join(repo, "app.py")
        with open(src, "w") as f:
            f.write(
                "def alpha():\n    return 1\n\n"
                "def beta():\n    return alpha() + 1\n"
            )
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-m", "initial")

        # Modify BOTH functions in one commit -> co-change ground truth
        with open(src, "w") as f:
            f.write(
                "def alpha():\n    return 2\n\n"
                "def beta():\n    return alpha() + 2\n"
            )
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-m", "change both")

        cases = cases_from_history(repo, max_cases=10)
        assert cases, "expected at least one co-change case"
        queries = {c.changed[0] for c in cases}
        assert queries <= {"./app.py:alpha", "./app.py:beta"}
        for c in cases:
            assert c.min_recall == 0.5   # history cases are noisy by design
            assert c.must_include        # the co-changed sibling(s)

        # And the full loop: run the generated cases against the pipeline.
        results = run_cases(repo, cases)
        assert all(r.recall == 1.0 for r in results), (
            "alpha/beta are direct call-graph neighbors; both must be retrieved"
        )
