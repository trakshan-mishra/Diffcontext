"""Bookkeeping guarantees of the rung-5 downstream harness.

These cover the parts that decide what counts as EVIDENCE — transient-error
quarantine, resume/dedupe, the sensitivity-gate screen, and the discrimination
diagnostic. A bug here does not crash the eval; it silently changes the numbers
a claim rests on, which is why they are pinned.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.downstream.run_eval import (
    _load_measurements,
    _report_rows,
    is_measurement,
    is_transient_error,
    load_screen,
    result_key,
)


def _row(commit, provider, passed, sample=0, **extra):
    r = {"commit": commit, "provider": provider, "sample": sample, "passed": passed}
    r.update(extra)
    return r


def _write(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


# ---- transient quarantine ----------------------------------------------------

def test_api_errors_are_transient_but_model_outcomes_are_not():
    assert is_transient_error(_row("c", "none", False, gen_error="api_error:http_429"))
    assert is_transient_error(_row("c", "none", False, gen_error="api_error:http_503"))
    # a model that answered without a diff genuinely failed the task
    assert not is_transient_error(_row("c", "none", False, gen_error="no_diff_in_output:"))
    assert not is_transient_error(_row("c", "none", True))


def test_transient_rows_are_dropped_from_measurements(tmp_path):
    path = _write(tmp_path, "r.jsonl", [
        _row("c1", "diffcontext", False, gen_error="api_error:http_429"),
        _row("c2", "diffcontext", True),
    ])
    rows, n_transient = _load_measurements(path)
    assert n_transient == 1
    assert [r["commit"] for r in rows] == ["c2"]


def test_retried_task_keeps_the_later_measurement(tmp_path):
    """A resumed run appends a fresh row; the retry must win, not the stale fail."""
    path = _write(tmp_path, "r.jsonl", [
        _row("c1", "diffcontext", False),
        _row("c1", "diffcontext", True),
    ])
    rows, _ = _load_measurements(path)
    assert len(rows) == 1 and rows[0]["passed"] is True


def test_result_key_separates_samples_and_arms():
    keys = {result_key(_row("c", "diffcontext", True, sample=0)),
            result_key(_row("c", "diffcontext", True, sample=1)),
            result_key(_row("c", "bm25", True, sample=0))}
    assert len(keys) == 3


def test_sidecar_rows_are_not_measurements():
    """`results/*.jsonl` sweeps up gold-gate skips and gate screens. The skip
    shape has no provider (it used to crash --report); the screen shape has
    provider='none' and would silently re-enter as the baseline arm it was
    deliberately removed from."""
    assert is_measurement(_row("c", "diffcontext", True))
    assert not is_measurement({"commit": "c", "skipped": "gold_fails_in_env"})
    assert not is_measurement(_row("c", "none", False, screen=True))


def test_report_ignores_skipped_and_screen_sidecar_files(tmp_path):
    path = _write(tmp_path, "r.jsonl", [
        {"commit": "c0", "skipped": "gold_fails_in_env", "detail": ""},
        _row("c1", "none", False, screen=True),
        _row("c1", "diffcontext", True),
    ])
    rows, _ = _load_measurements(path)
    assert [(r["commit"], r["provider"]) for r in rows] == [("c1", "diffcontext")]


# ---- sensitivity gate --------------------------------------------------------

def test_screen_records_verdict_per_commit(tmp_path):
    path = _write(tmp_path, "s.screen.jsonl", [
        _row("solved", "none", True, screen=True),
        _row("hard", "none", False, screen=True),
    ])
    assert load_screen(path) == {"solved": True, "hard": False}


def test_screen_ignores_transient_so_a_429_is_retried_not_cached(tmp_path):
    """A rate-limited screen is 'unknown', never 'the model failed it'. Caching
    it as a verdict would admit an unscreened task into the measured set."""
    path = _write(tmp_path, "s.screen.jsonl", [
        _row("c1", "none", False, screen=True, gen_error="api_error:http_429"),
    ])
    assert load_screen(path) == {}


def test_screen_of_missing_file_is_empty(tmp_path):
    assert load_screen(str(tmp_path / "absent.jsonl")) == {}


# ---- unattended sweep driver accounting -------------------------------------

def _sweep_env(tmp_path, monkeypatch, commits):
    """Point the driver's module-level dirs at a scratch tree with one repo."""
    from benchmarks.downstream import auto_free_sweep as afs
    results, tasks = tmp_path / "results", tmp_path / "tasks"
    results.mkdir(), tasks.mkdir()
    (tasks / "demo.json").write_text(
        json.dumps({"tasks": [{"commit": c} for c in commits]}), encoding="utf-8")
    monkeypatch.setattr(afs, "RESULTS_DIR", str(results))
    monkeypatch.setattr(afs, "TASKS_DIR", str(tasks))
    return afs, results


def test_driver_stops_counting_screened_out_commits(tmp_path, monkeypatch):
    """The forever-loop guard: a task retired by the gate never receives per-arm
    rows, so if `remaining` still counted it the driver would sleep through the
    cooldown forever instead of finishing."""
    afs, results = _sweep_env(tmp_path, monkeypatch, ["solved", "hard"])
    (results / "demo.t.screen.jsonl").write_text(
        json.dumps(_row("solved", "none", True, screen=True)) + "\n", encoding="utf-8")
    (results / "demo.t.jsonl").write_text("".join(
        json.dumps(_row("hard", p, False)) + "\n" for p in ("diffcontext", "bm25")),
        encoding="utf-8")
    # 'hard' is fully measured, 'solved' is retired -> nothing left to do
    assert afs.remaining(["demo"], ["diffcontext", "bm25"], 1, "t") == 0


def test_driver_still_owes_work_for_unscreened_commits(tmp_path, monkeypatch):
    afs, results = _sweep_env(tmp_path, monkeypatch, ["solved", "hard"])
    (results / "demo.t.screen.jsonl").write_text(
        json.dumps(_row("solved", "none", True, screen=True)) + "\n", encoding="utf-8")
    assert afs.remaining(["demo"], ["diffcontext", "bm25"], 1, "t") == 2  # 'hard' x 2 arms


def test_driver_ignores_rate_limited_screens(tmp_path, monkeypatch):
    """A 429'd screen is not a verdict — the commit still owes measurements."""
    afs, results = _sweep_env(tmp_path, monkeypatch, ["c1"])
    (results / "demo.t.screen.jsonl").write_text(
        json.dumps(_row("c1", "none", False, screen=True,
                        gen_error="api_error:http_429")) + "\n", encoding="utf-8")
    assert afs.remaining(["demo"], ["diffcontext", "bm25"], 1, "t") == 2


# ---- discrimination diagnostic ----------------------------------------------

def test_reports_zero_discrimination_when_every_arm_ties(capsys):
    """The failure mode this diagnostic exists to catch: arms differ on paper
    but no single task separates them, so the null is about the task set."""
    rows = []
    for c in ("t1", "t2"):                       # both solved by everyone
        for p in ("diffcontext", "bm25"):
            rows.append(_row(c, p, True))
    for c in ("t3", "t4"):                       # both solved by no one
        for p in ("diffcontext", "bm25"):
            rows.append(_row(c, p, False))
    _report_rows(rows, "tie")
    out = capsys.readouterr().out
    assert "discrimination: 0/4 tasks separate the arms" in out
    assert "2 solved by all = ceiling" in out
    assert "2 solved by none = floor" in out
    assert "cannot support ANY claim" in out


def test_counts_only_tasks_where_arms_actually_disagree(capsys):
    rows = [
        _row("ceil", "diffcontext", True),  _row("ceil", "bm25", True),
        _row("floor", "diffcontext", False), _row("floor", "bm25", False),
        _row("real", "diffcontext", True),  _row("real", "bm25", False),
    ]
    _report_rows(rows, "mixed")
    out = capsys.readouterr().out
    assert "discrimination: 1/3 tasks separate the arms" in out
    assert "cannot support ANY claim" not in out
