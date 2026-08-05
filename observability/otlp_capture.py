#!/usr/bin/env python3
"""Local OTLP/HTTP receiver — captures what the neatlogs SDK puts on the wire,
decodes the protobuf, and dumps JSON for inspection.

    python otlp_capture.py out.json 4318
"""
import gzip
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/otlp_capture.json"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4318

captured = []


def _attrs(kvs):
    out = {}
    for kv in kvs:
        v = kv.value
        for f in ("string_value", "int_value", "double_value", "bool_value"):
            if v.HasField(f):
                out[kv.key] = getattr(v, f)
                break
        else:
            out[kv.key] = str(v)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        raw_len = len(body)
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)

        req = trace_service_pb2.ExportTraceServiceRequest()
        req.ParseFromString(body)

        batch = {"path": self.path, "wire_bytes": raw_len,
                 "decoded_bytes": len(body), "spans": []}
        for rs in req.resource_spans:
            res = _attrs(rs.resource.attributes)
            for ss in rs.scope_spans:
                for sp in ss.spans:
                    batch["spans"].append({
                        "name": sp.name,
                        "trace_id": sp.trace_id.hex(),
                        "span_id": sp.span_id.hex(),
                        "parent_span_id": sp.parent_span_id.hex() or None,
                        "start_unix_nano": sp.start_time_unix_nano,
                        "end_unix_nano": sp.end_time_unix_nano,
                        "duration_ms": (sp.end_time_unix_nano
                                        - sp.start_time_unix_nano) / 1e6,
                        "kind": sp.kind,
                        "scope": ss.scope.name,
                        "attributes": _attrs(sp.attributes),
                        "status_code": sp.status.code,
                        "status_message": sp.status.message,
                        "events": [{"name": e.name,
                                    "attributes": _attrs(e.attributes)}
                                   for e in sp.events],
                        "service": res.get("service.name"),
                    })
        captured.append(batch)
        with open(OUT, "w") as f:
            json.dump(captured, f, indent=2, default=str)

        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        resp = trace_service_pb2.ExportTraceServiceResponse().SerializeToString()
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


if __name__ == "__main__":
    print(f"OTLP capture on 127.0.0.1:{PORT} -> {OUT}", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
