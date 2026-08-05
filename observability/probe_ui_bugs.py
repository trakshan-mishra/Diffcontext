#!/usr/bin/env python3
"""probe_ui_bugs.py — minimal, self-evidencing reproducers for the Neatlogs
issues found while instrumenting DiffContext.

Span NAMES encode the ground truth, so a dashboard screenshot needs no caption.

Probe 1 (child ordering): three children run A -> B -> C with durations that are
deliberately NOT monotonic (300ms, 20ms, 150ms). This separates hypotheses about
what the dashboard is actually sorting on:

    renders A,B,C  -> correct
    renders C,B,A  -> children reversed
    renders A,C,B  -> sorted by duration desc
    renders B,C,A  -> sorted by duration asc

Probe 2 (generator): @span on a generator closes the span when the generator
OBJECT is created, not when it is consumed — so ~450ms of real work reports as
~0.2ms. Relevant well beyond this repo: streamed LLM responses are generators.

Run against the real backend:
    python observability/probe_ui_bugs.py

Run against a local capture instead (no network, no key):
    python observability/otlp_capture.py /tmp/probe.json 4318 &
    NEATLOGS_ENDPOINT_FORCE=http://127.0.0.1:4318 \
        python observability/probe_ui_bugs.py
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

_env_path = os.path.join(HERE, ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

if not os.environ.get("NEATLOGS_API_KEY"):
    sys.exit("NEATLOGS_API_KEY is not set (see observability/README.md).")

import neatlogs  # noqa: E402
from neatlogs import span  # noqa: E402
from opentelemetry import trace as otel_trace  # noqa: E402

# init() ignores $NEATLOGS_ENDPOINT (see README finding 3) — pass it explicitly.
_init_kwargs = dict(
    api_key=os.environ["NEATLOGS_API_KEY"],
    workflow_name="diffcontext-uibug-probe",
    tags=["probe:span-ordering", "purpose:bug-report"],
)
_forced = os.environ.get("NEATLOGS_ENDPOINT_FORCE")
if _forced:
    _init_kwargs["endpoint"] = _forced

neatlogs.init(**_init_kwargs)

TRACE_IDS = {}


def _record(label):
    ctx = otel_trace.get_current_span().get_span_context()
    TRACE_IDS[label] = format(ctx.trace_id, "032x")


# --------------------------------------------------------------------------
# Probe 1 — child span ordering
# --------------------------------------------------------------------------

@span(kind="CHAIN", name="A_runs_first_300ms")
def a_first():
    time.sleep(0.30)


@span(kind="CHAIN", name="B_runs_second_020ms")
def b_second():
    time.sleep(0.02)


@span(kind="CHAIN", name="C_runs_third_150ms")
def c_third():
    time.sleep(0.15)


@span(kind="WORKFLOW", name="ordering_probe_A_then_B_then_C")
def ordering_probe():
    _record("ordering")
    a_first()
    b_second()
    c_third()


# --------------------------------------------------------------------------
# Probe 2 — generator span closes at creation, not consumption
# --------------------------------------------------------------------------

@span(kind="CHAIN", name="generator_real_work_450ms")
def slow_gen():
    for i in range(3):
        time.sleep(0.15)
        yield i


@span(kind="WORKFLOW", name="generator_probe_expect_450ms_child")
def generator_probe():
    _record("generator")
    return sum(slow_gen())


def main():
    t0 = time.perf_counter()
    ordering_probe()
    generator_probe()
    wall = (time.perf_counter() - t0) * 1000

    neatlogs.flush()
    neatlogs.shutdown()

    print(f"\nwall clock: {wall:.0f}ms")
    for label, tid in TRACE_IDS.items():
        print(f"  {label:<10} trace_id = {tid}")


if __name__ == "__main__":
    main()
