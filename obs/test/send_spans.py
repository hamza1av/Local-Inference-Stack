#!/usr/bin/env python3
"""Send synthetic OTLP/JSON traces (gateway-like gen_ai.* + langfuse.* spans) to the recorder.

    python3 obs/test/send_spans.py [--url URL] [--app NAME] [--traces N] [--path langfuse|otlp]
                                   [--service NAME] [--no-key] [--pad BYTES]

Stdlib only. Each trace: a root "chat" span + a generation span (+ an error tool span
every third trace). Prints the trace ids it sent.
"""
import argparse, json, os, random, time, urllib.request


def attr(k, v):
    if isinstance(v, bool):
        val = {"boolValue": v}
    elif isinstance(v, int):
        val = {"intValue": str(v)}
    elif isinstance(v, float):
        val = {"doubleValue": v}
    elif isinstance(v, list):
        val = {"arrayValue": {"values": [{"stringValue": x} for x in v]}}
    else:
        val = {"stringValue": str(v)}
    return {"key": k, "value": val}


def span(trace_id, name, start, dur_ms, attrs, parent="", error=False):
    s = {
        "traceId": trace_id, "spanId": os.urandom(8).hex(), "name": name, "kind": 3,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + dur_ms * 1_000_000),
        "attributes": [attr(k, v) for k, v in attrs.items()],
        "status": {"code": 2, "message": "tool failed: timeout"} if error else {"code": 1},
    }
    if parent:
        s["parentSpanId"] = parent
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:" + os.environ.get("RECORDER_PORT", "4320"))
    ap.add_argument("--app", default="testapp")
    ap.add_argument("--service", default="")
    ap.add_argument("--traces", type=int, default=5)
    ap.add_argument("--path", choices=["langfuse", "otlp"], default="langfuse")
    ap.add_argument("--no-key", action="store_true")
    ap.add_argument("--pad", type=int, default=0, help="extra bytes of prompt text per generation")
    a = ap.parse_args()

    path = "/api/public/otel/v1/traces" if a.path == "langfuse" else "/v1/traces"
    spans, ids = [], []
    now = time.time_ns()
    for i in range(a.traces):
        tid = os.urandom(16).hex(); ids.append(tid)
        t0 = now - (a.traces - i) * 60_000_000_000
        root = span(tid, "chat", t0, 1500 + 100 * i, {
            "langfuse.trace.name": f"{a.app}-chat", "langfuse.user.id": "user-1",
            "langfuse.session.id": "sess-1", "langfuse.trace.tags": ["synthetic"],
        })
        model = random.choice(["chat", "task"])
        inp, out = 100 + 10 * i, 20 + 5 * i
        gen = span(tid, f"chat {model}", t0 + 10_000_000, 1200 + 100 * i, {
            "gen_ai.operation.name": "chat", "gen_ai.system": "uzu",
            "gen_ai.request.model": model, "gen_ai.response.model": model,
            "gen_ai.usage.input_tokens": inp, "gen_ai.usage.output_tokens": out,
            "langfuse.observation.type": "generation",
            "langfuse.observation.input": json.dumps([{"role": "user", "content": "hi" + "x" * a.pad}]),
            "langfuse.observation.output": json.dumps({"role": "assistant", "content": "hello"}),
        }, parent=root["spanId"])
        spans += [root, gen]
        if i % 3 == 2:
            spans.append(span(tid, "tool web_search", t0 + 20_000_000, 300, {
                "langfuse.observation.type": "tool", "langfuse.observation.level": "ERROR",
            }, parent=root["spanId"], error=True))

    res = [attr("telemetry.sdk.language", "python")]
    if a.service:
        res.append(attr("service.name", a.service))
    body = {"resourceSpans": [{"resource": {"attributes": res},
                               "scopeSpans": [{"scope": {"name": "stack-test"}, "spans": spans}]}]}
    headers = {"Content-Type": "application/json"}
    if not a.no_key:
        headers["x-langfuse-public-key"] = f"pk-lf-{a.app}"
    req = urllib.request.Request(a.url + path, json.dumps(body).encode(), headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        print(r.status, r.read().decode()[:200])
    print("\n".join(ids))


if __name__ == "__main__":
    main()
