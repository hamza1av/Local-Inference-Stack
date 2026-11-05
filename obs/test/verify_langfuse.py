#!/usr/bin/env -S python3 -u
"""Poll the Langfuse public API until every expected trace is there with its app tag.

    verify_langfuse.py [--timeout S] app:TRACEID [app:TRACEID ...]

Langfuse v4 runs in "events only" mode: GET /api/public/traces is gone, so this reads
GET /api/public/v2/observations (basic auth with the project keys) and checks the root
observation's trace tags (metadata "attributes.langfuse.trace.tags"). Exit 0 when all
expected traces carry tag app:<app>.
"""
import argparse, base64, json, os, sys, time, urllib.error, urllib.parse, urllib.request

base = os.environ.get("LANGFUSE_URL", "http://127.0.0.1:" + os.environ.get("LANGFUSE_PORT", "3020"))
auth = base64.b64encode(f'{os.environ["LANGFUSE_PUBLIC_KEY"]}:{os.environ["LANGFUSE_SECRET_KEY"]}'.encode()).decode()


def observations():
    out, cursor = [], None
    while True:
        p = {"fromStartTime": "2000-01-01T00:00:00Z", "limit": 1000, "fields": "core,basic,metadata"}
        if cursor:
            p["cursor"] = cursor
        req = urllib.request.Request(f"{base}/api/public/v2/observations?" + urllib.parse.urlencode(p),
                                     headers={"Authorization": "Basic " + auth})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        out += d.get("data", [])
        cursor = (d.get("meta") or {}).get("cursor")
        if not cursor or not d.get("data"):
            return out


def tags_by_trace(obs):
    tags = {}
    for o in obs:
        t = (o.get("metadata") or {}).get("attributes.langfuse.trace.tags") or []
        if isinstance(t, str):
            t = [t]
        tags.setdefault(o["traceId"], set()).update(t)
    return tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("expect", nargs="+", help="app:traceid")
    a = ap.parse_args()
    want = [e.rsplit(":", 1) for e in a.expect]
    t0, missing = time.time(), want
    while time.time() - t0 < a.timeout:
        try:
            tags = tags_by_trace(observations())
        except (urllib.error.URLError, OSError) as e:
            print("  api not ready:", e); time.sleep(5); continue
        missing = [(app, tid) for app, tid in want if f"app:{app}" not in tags.get(tid, set())]
        print(f"  {len(want) - len(missing)}/{len(want)} expected traces present with their app tag")
        if not missing:
            apps = sorted({t for ts in tags.values() for t in ts if t.startswith("app:")})
            print("  app tags in Langfuse:", ", ".join(apps))
            return 0
        time.sleep(5)
    print("missing:", missing, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
