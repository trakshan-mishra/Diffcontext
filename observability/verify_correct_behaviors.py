#!/usr/bin/env python3
"""verify_correct_behaviors.py — the other half of the audit.

The findings in README.md say what is broken. This script checks the four
things that are *not* broken — the places tracing SDKs usually fail — so the
claim "these are correct" is backed by a run rather than by memory.

    python observability/verify_correct_behaviors.py

No API key, no network, no dashboard: an in-process OTLP receiver decodes the
protobuf the SDK actually exports, and every assertion is made against that.

  1. thread-pool context propagation — spans made inside worker threads must
     attach to the span that submitted the work, not to nothing.
  2. asyncio.gather propagation — same, across concurrently awaited coroutines.
  3. deep nesting (6 levels) — each span's parent must be the level above it.
  4. exception capture — OTel status ERROR (2) plus an `exception` event
     carrying type, message and stacktrace. There are no error *attributes*,
     which is easy to misread as "exceptions aren't recorded".
"""

import asyncio
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 4322
CAPTURED = []


def _start_receiver():
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
                            "span_id": sp.span_id.hex(),
                            "parent_span_id": sp.parent_span_id.hex() or None,
                            "trace_id": sp.trace_id.hex(),
                            "status_code": sp.status.code,
                            "events": [
                                {
                                    "name": e.name,
                                    "attrs": {a.key: a.value.string_value
                                              for a in e.attributes},
                                }
                                for e in sp.events
                            ],
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
    workflow_name="correct-behaviors-audit",
    endpoint=f"http://127.0.0.1:{PORT}",   # init() ignores $NEATLOGS_ENDPOINT
)


# --- 1. thread-pool propagation --------------------------------------------
@span(kind="CHAIN", name="threadpool_child")
def _threadpool_child(i):
    time.sleep(0.01)
    return i


@span(kind="WORKFLOW", name="threadpool_parent")
def threadpool_case():
    with ThreadPoolExecutor(max_workers=3) as ex:
        return list(ex.map(_threadpool_child, range(3)))


# --- 2. asyncio.gather propagation -----------------------------------------
@span(kind="CHAIN", name="gather_child")
async def _gather_child(i):
    await asyncio.sleep(0.01)
    return i


@span(kind="WORKFLOW", name="gather_parent")
async def gather_case():
    return await asyncio.gather(*(_gather_child(i) for i in range(3)))


# --- 3. six-deep nesting ----------------------------------------------------
@span(kind="CHAIN", name="depth_6")
def _d6():
    time.sleep(0.005)


@span(kind="CHAIN", name="depth_5")
def _d5():
    _d6()


@span(kind="CHAIN", name="depth_4")
def _d4():
    _d5()


@span(kind="CHAIN", name="depth_3")
def _d3():
    _d4()


@span(kind="CHAIN", name="depth_2")
def _d2():
    _d3()


@span(kind="WORKFLOW", name="depth_1")
def nesting_case():
    _d2()


# --- 4. exception capture ---------------------------------------------------
@span(kind="CHAIN", name="raises_valueerror")
def raising_case():
    raise ValueError("deliberate failure for the audit")


def main():
    threadpool_case()
    asyncio.run(gather_case())
    nesting_case()
    try:
        raising_case()
    except ValueError:
        pass

    neatlogs.flush()
    neatlogs.shutdown()
    time.sleep(0.6)

    by_id = {s["span_id"]: s for s in CAPTURED}
    named = {}
    for s in CAPTURED:
        named.setdefault(s["name"], []).append(s)

    def parent_name(s):
        p = by_id.get(s["parent_span_id"])
        return p["name"] if p else None

    results = []

    # 1. thread pool
    kids = named.get("threadpool_child", [])
    parents = named.get("threadpool_parent", [])
    ok = (len(kids) == 3 and len(parents) == 1
          and all(k["parent_span_id"] == parents[0]["span_id"] for k in kids)
          and all(k["trace_id"] == parents[0]["trace_id"] for k in kids))
    results.append(("thread-pool propagation",
                    f"{len(kids)}/3 children attached to submitter", ok))

    # 2. asyncio.gather
    kids = named.get("gather_child", [])
    parents = named.get("gather_parent", [])
    ok = (len(kids) == 3 and len(parents) == 1
          and all(k["parent_span_id"] == parents[0]["span_id"] for k in kids)
          and all(k["trace_id"] == parents[0]["trace_id"] for k in kids))
    results.append(("asyncio.gather propagation",
                    f"{len(kids)}/3 children attached to awaiting parent", ok))

    # 3. six-deep nesting
    chain_ok = True
    for depth in range(2, 7):
        got = named.get(f"depth_{depth}", [])
        if len(got) != 1 or parent_name(got[0]) != f"depth_{depth - 1}":
            chain_ok = False
    root = named.get("depth_1", [])
    chain_ok = chain_ok and len(root) == 1
    results.append(("6-deep nesting parentage",
                    "depth_1 -> ... -> depth_6 chain intact" if chain_ok
                    else "chain broken", chain_ok))

    # 4. exception capture
    got = named.get("raises_valueerror", [])
    ok = False
    detail = "span missing"
    if got:
        s = got[0]
        ev = next((e for e in s["events"] if e["name"] == "exception"), None)
        has_status = s["status_code"] == 2
        if ev:
            a = ev["attrs"]
            has_all = all(a.get(k) for k in ("exception.type",
                                             "exception.message",
                                             "exception.stacktrace"))
            ok = has_status and has_all
            detail = (f"status={s['status_code']} event=exception "
                      f"type={a.get('exception.type')!r} "
                      f"msg/stack={'present' if has_all else 'INCOMPLETE'}")
        else:
            detail = f"status={s['status_code']} but no `exception` event"
    results.append(("exception capture", detail, ok))

    print()
    print(f"  neatlogs {neatlogs.__version__}   python "
          f"{sys.version_info.major}.{sys.version_info.minor}."
          f"{sys.version_info.micro}   {len(CAPTURED)} spans captured")
    print()
    print(f"  {'behavior':<28} {'verdict':<8} detail")
    print(f"  {'-' * 28} {'-' * 8} {'-' * 44}")
    failed = 0
    for name, detail, ok in results:
        if not ok:
            failed += 1
        print(f"  {name:<28} {'CORRECT' if ok else 'BROKEN':<8} {detail}")
    print()
    print("  All four hold — these are the usual failure points, and they pass."
          if not failed else
          f"  {failed} behavior(s) did NOT hold — do not claim these as correct.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
