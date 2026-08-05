#!/usr/bin/env python3
"""prove_generator_bug.py — self-contained proof of the @span generator bug.

No API key, no network, no dashboard. Stands up an in-process OTLP receiver,
runs three spans whose real durations are known by construction, and prints the
measured span duration next to the wall clock actually spent.

    python observability/prove_generator_bug.py

Expected: the plain function's span matches its wall clock; the sync generator
and the async generator both report ~0ms for ~450ms of real work.

Why it happens (neatlogs 1.4.16, decorators/_base.py):
  _decorate_span() branches ONLY on inspect.iscoroutinefunction(func) (line 216).
  That is False for both generator functions and async-generator functions, so
  both fall through to sync_wrapper. There, `result = func(*args, **kwargs)`
  (line 322) returns a generator OBJECT without executing any of the body, the
  `with neatlogs_span(...)` block exits, and the span ends -- before the caller
  has iterated anything.
"""

import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 4319
CAPTURED = []


def _start_receiver():
    """Minimal OTLP/HTTP receiver: decode protobuf, keep name+duration."""
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("Content-Encoding") == "gzip":
                import gzip
                body = gzip.decompress(body)
            req = trace_service_pb2.ExportTraceServiceRequest()
            req.ParseFromString(body)
            for rs in req.resource_spans:
                for ss in rs.scope_spans:
                    for sp in ss.spans:
                        CAPTURED.append({
                            "name": sp.name,
                            "ms": (sp.end_time_unix_nano
                                   - sp.start_time_unix_nano) / 1e6,
                        })
            self.send_response(200)
            resp = trace_service_pb2.ExportTraceServiceResponse().SerializeToString()
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    srv = HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


srv = _start_receiver()

import neatlogs  # noqa: E402
from neatlogs import span  # noqa: E402

neatlogs.init(
    api_key=os.environ.get("NEATLOGS_API_KEY", "local-capture-no-key-needed"),
    workflow_name="generator-bug-proof",
    endpoint=f"http://127.0.0.1:{PORT}",   # init() ignores $NEATLOGS_ENDPOINT
)

WORK = 0.15   # seconds per chunk, 3 chunks = 450ms


# --- control: a plain function doing the same work -------------------------
@span(kind="CHAIN", name="control_plain_function_450ms")
def plain():
    total = 0
    for i in range(3):
        time.sleep(WORK)
        total += i
    return total


# --- subject 1: sync generator ---------------------------------------------
@span(kind="CHAIN", name="sync_generator_450ms")
def sync_gen():
    for i in range(3):
        time.sleep(WORK)
        yield i


# --- subject 2: async generator (what a streamed LLM response is) ----------
@span(kind="CHAIN", name="async_generator_450ms")
async def async_gen():
    import asyncio
    for i in range(3):
        await asyncio.sleep(WORK)
        yield i


def main():
    results = []

    t0 = time.perf_counter()
    plain()
    results.append(("control_plain_function_450ms", (time.perf_counter() - t0) * 1000))

    t0 = time.perf_counter()
    sum(sync_gen())
    results.append(("sync_generator_450ms", (time.perf_counter() - t0) * 1000))

    import asyncio

    async def drain():
        return [x async for x in async_gen()]

    t0 = time.perf_counter()
    asyncio.run(drain())
    results.append(("async_generator_450ms", (time.perf_counter() - t0) * 1000))

    neatlogs.flush()
    neatlogs.shutdown()
    time.sleep(0.6)   # let the exporter drain to the local receiver

    by_name = {}
    for s in CAPTURED:
        by_name.setdefault(s["name"], s["ms"])

    print()
    print(f"  neatlogs {neatlogs.__version__}   python "
          f"{sys.version_info.major}.{sys.version_info.minor}."
          f"{sys.version_info.micro}")
    print()
    print(f"  {'span':<34} {'real work':>10} {'span says':>11}   verdict")
    print(f"  {'-'*34} {'-'*10} {'-'*11}   {'-'*7}")
    failures = 0
    for name, wall in results:
        got = by_name.get(name)
        if got is None:
            print(f"  {name:<34} {wall:>9.0f}ms {'MISSING':>11}   ?")
            continue
        ok = abs(got - wall) < 100
        if not ok:
            failures += 1
        print(f"  {name:<34} {wall:>9.0f}ms {got:>10.1f}ms   "
              f"{'ok' if ok else 'WRONG'}")
    print()
    if failures:
        print(f"  {failures} span(s) report a duration unrelated to the work done.")
        print("  Cause: decorators/_base.py:216 branches only on "
              "iscoroutinefunction();")
        print("  generator and async-generator functions fall through to "
              "sync_wrapper,")
        print("  whose `result = func(...)` (line 322) returns an unconsumed "
              "iterator.")
    else:
        print("  All spans matched their wall clock — bug not reproduced here.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
