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

from benchmarks.downstream.providers import _estimate_tokens, render_context
from benchmarks.downstream.run_eval import (
    _load_measurements,
    _report_rows,
    is_measurement,
    is_transient_error,
    load_screen,
    report,
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


def test_result_key_separates_models():
    """Two models answering the same (task, arm, sample) are two measurements,
    not a retry. Without the model in the key the resume set skipped rows another
    model had written, and dedup kept only the last model to touch the file."""
    a = _row("c", "diffcontext", True, model="gemini-flash-latest")
    b = _row("c", "diffcontext", False, model="llama-3.3-70b")
    assert result_key(a) != result_key(b)


def test_resume_key_matches_the_key_the_writer_builds():
    """Resume compares a key built from an about-to-run row against keys read
    back out of the results file. If those two constructions drift, nothing
    crashes — resume just stops matching and every run re-measures everything,
    which on a quota-bound free tier is the difference between finishing and
    never finishing. Pin that both sides agree, including the mock shape where
    model is None."""
    read_back = _row("c1", "diffcontext", True, model="m", backend="gemini")
    about_to_run = {"commit": "c1", "repo": "r", "provider": "diffcontext",
                    "sample": 0, "backend": "gemini", "model": "m",
                    "context_tokens_budget": 4000, "n_seeds": 3, "ts": 123.0}
    assert result_key(about_to_run) == result_key(read_back)

    mock_read_back = _row("c1", "diffcontext", True, model=None, backend=None)
    mock_about_to_run = dict(about_to_run, model=None, backend=None)
    assert result_key(mock_about_to_run) == result_key(mock_read_back)


def test_two_models_in_one_file_are_not_collapsed(tmp_path):
    """--tag is meant to give each model its own file but does not enforce it;
    results/requests.gemini25.jsonl really does hold two. Both must survive."""
    path = _write(tmp_path, "r.jsonl", [
        _row("c1", "diffcontext", True, model="m1"),
        _row("c1", "diffcontext", False, model="m2"),
    ])
    rows, _ = _load_measurements(path)
    assert sorted((r["model"], r["passed"]) for r in rows) == [("m1", True), ("m2", False)]


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


def test_report_warns_when_one_file_mixes_models(tmp_path, capsys):
    """A mixed file still reports correctly, but it breaks per-model resume, so
    the reader is told rather than left to find out from the row counts."""
    path = _write(tmp_path, "r.jsonl", [
        _row("c1", "diffcontext", True, model="m1"),
        _row("c1", "bm25", False, model="m2"),
    ])
    report([path])
    out = capsys.readouterr().out
    assert "warning: this file mixes 2 models" in out
    assert "m1" in out and "m2" in out


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
    assert afs.remaining(["demo"], ["diffcontext", "bm25"], 1, "t", None) == 0


def test_driver_still_owes_work_for_unscreened_commits(tmp_path, monkeypatch):
    afs, results = _sweep_env(tmp_path, monkeypatch, ["solved", "hard"])
    (results / "demo.t.screen.jsonl").write_text(
        json.dumps(_row("solved", "none", True, screen=True)) + "\n", encoding="utf-8")
    assert afs.remaining(["demo"], ["diffcontext", "bm25"], 1, "t", None) == 2  # 'hard' x 2 arms


def test_driver_does_not_retire_work_using_another_models_rows(tmp_path, monkeypatch):
    """If a tag file picks up a second model, its rows must not count as this
    model's progress — otherwise the driver prints ALL DONE with measurements
    it never made."""
    afs, results = _sweep_env(tmp_path, monkeypatch, ["c1"])
    (results / "demo.t.jsonl").write_text("".join(
        json.dumps(_row("c1", p, True, model="other-model")) + "\n"
        for p in ("diffcontext", "bm25")), encoding="utf-8")
    # 'mine' has measured nothing, so it still owes both arms
    assert afs.remaining(["demo"], ["diffcontext", "bm25"], 1, "t", "mine") == 2
    # ...and the model that did the work owes nothing
    assert afs.remaining(["demo"], ["diffcontext", "bm25"], 1, "t", "other-model") == 0


def test_driver_ignores_rate_limited_screens(tmp_path, monkeypatch):
    """A 429'd screen is not a verdict — the commit still owes measurements."""
    afs, results = _sweep_env(tmp_path, monkeypatch, ["c1"])
    (results / "demo.t.screen.jsonl").write_text(
        json.dumps(_row("c1", "none", False, screen=True,
                        gen_error="api_error:http_429")) + "\n", encoding="utf-8")
    assert afs.remaining(["demo"], ["diffcontext", "bm25"], 1, "t", None) == 2


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


def test_pooling_across_models_cannot_invent_discrimination(capsys):
    """Regression for the defect that made the pooled report unsafe to read.

    Two models, same commits, arms tied within each model: a strong model passes
    everything, a weak one fails everything. No arm ever separates from another,
    so the honest answer is 0 informative tasks.

    Keying the paired unit on commit alone averaged the two models into one cell
    (1.0 and 0.0 -> 0.5 for EVERY arm), which is neither all-pass nor all-fail,
    so every task counted as 'separating the arms'. That is how the July corpus
    printed a pooled `discrimination: 3/4` while all four per-model files
    printed 0/N. Pure model disagreement, reported as a retrieval effect.
    """
    rows = []
    for commit in ("t1", "t2", "t3"):
        for provider in ("diffcontext", "bm25"):
            rows.append(_row(commit, provider, True, model="strong"))
            rows.append(_row(commit, provider, False, model="weak"))
    _report_rows(rows, "pooled")
    out = capsys.readouterr().out
    assert "discrimination: 0/6 tasks separate the arms" in out
    assert "3 solved by all = ceiling" in out
    assert "3 solved by none = floor" in out
    assert "cannot support ANY claim" in out
    # and the unit is named honestly once more than one model is in play
    assert "task-instances (2 models x task)" in out


def test_within_one_model_real_disagreement_still_counts(capsys):
    """The fix must not over-correct: a genuine arm split inside one model is
    still discrimination."""
    rows = [
        _row("real", "diffcontext", True, model="m"),
        _row("real", "bm25", False, model="m"),
        _row("floor", "diffcontext", False, model="m"),
        _row("floor", "bm25", False, model="m"),
    ]
    _report_rows(rows, "one-model")
    out = capsys.readouterr().out
    assert "discrimination: 1/2 tasks separate the arms" in out
    assert "task-instances" not in out          # single model -> plain "tasks"


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


# ---- budget is hard ----------------------------------------------------------
# The whole rung-5 design holds the context budget fixed and varies only the
# provider. A renderer that overshoots gives some arms more real tokens than
# their budget says, which is a fairness defect in the comparison itself.

class _Sym:
    def __init__(self, code):
        self.code = code


class _Index:
    def __init__(self, symbols):
        self.symbols = symbols


def test_rendered_context_never_exceeds_the_budget():
    # Many small blocks: the case where join separators and per-block
    # flooring accumulate fastest.
    index = _Index({f"./m.py:f{i}": _Sym("def f():\n    return 1\n") for i in range(200)})
    ranked = list(index.symbols)
    for budget in (50, 100, 250, 500, 1000, 2000):
        text = render_context(index, ranked, budget)
        assert _estimate_tokens(text) <= budget, (
            f"budget={budget} produced {_estimate_tokens(text)} tokens"
        )


def test_budget_accounts_for_the_join_separators():
    # Two blocks that fit individually and by per-block sum, but not once
    # the joiner is counted, must not both be emitted.
    block = "x" * 100
    index = _Index({"./a.py:a": _Sym(block), "./b.py:b": _Sym(block)})
    budget = _estimate_tokens(f"# ./a.py:a\n{block}\n" * 2)
    text = render_context(index, ["./a.py:a", "./b.py:b"], budget)
    assert _estimate_tokens(text) <= budget
