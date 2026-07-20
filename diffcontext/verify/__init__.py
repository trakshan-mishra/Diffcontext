"""
diffcontext.verify — sufficiency scoring and user-defined retrieval test cases.

Turns "here are relevant files" into "this context is sufficient, with
measured confidence":

    from diffcontext.verify import (
        analyze_sufficiency,      # structural sufficiency of one compile
        load_cases, run_cases,    # user-defined expectations, measured
        cases_from_history,       # auto ground truth from git co-change
        calibrate,                # does the score track measured recall?
    )

See docs/VERIFY.md for the case file format and the honesty contract.
"""

from .sufficiency import (
    SufficiencyFinding,
    SufficiencyReport,
    analyze_sufficiency,
    HIGH_SCORE_THRESHOLD,
)
from .cases import (
    Case,
    CaseResult,
    CaseFormatError,
    Calibration,
    CalibrationBucket,
    CALIBRATION_FILENAME,
    load_cases,
    save_cases,
    run_cases,
    cases_from_history,
    calibrate,
    fit_recall_model,
    predict_recall,
    save_calibration,
    load_calibration,
    render_results,
    render_calibration,
)
from .history import CoChangeCase, extract_cochange_cases

__all__ = [
    "SufficiencyFinding",
    "SufficiencyReport",
    "analyze_sufficiency",
    "HIGH_SCORE_THRESHOLD",
    "Case",
    "CaseResult",
    "CaseFormatError",
    "Calibration",
    "CalibrationBucket",
    "CALIBRATION_FILENAME",
    "load_cases",
    "save_cases",
    "run_cases",
    "cases_from_history",
    "calibrate",
    "fit_recall_model",
    "predict_recall",
    "save_calibration",
    "load_calibration",
    "render_results",
    "render_calibration",
    "CoChangeCase",
    "extract_cochange_cases",
]
