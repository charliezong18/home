#!/usr/bin/env python3
"""dibao 命令行（给 Hermes / Claude 派工用，urllib，无依赖）。

  cli.py [--port 8766] health
  cli.py edits --json '{"target":"p:x","fields":{"next":"…"},"from":"claude"}'
  cli.py readings-add --url URL [--why 理由]      （readings 为旧别名）
  cli.py notes-pending
  cli.py note-done --id ID [--result 文本]
  cli.py collect [--sources menxia,dashboard]     立即触发一轮采集，返回 job id（GET /v1/collect/{job} 查）
输出响应 JSON；HTTP 非 2xx 退出码 1。
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def call(port, method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8766)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    sub.add_parser("notes-pending")
    sub.add_parser("edits").add_argument("--json", required=True)
    p = sub.add_parser("readings-add", aliases=["readings"])
    p.add_argument("--url", required=True)
    p.add_argument("--why", default="")
    p = sub.add_parser("note-done")
    p.add_argument("--id", dest="id", required=True)
    p.add_argument("--result", default="")
    p = sub.add_parser("collect")
    p.add_argument("--sources", default="", help="逗号分隔（menxia,dashboard），缺省 = 全部")
    a = ap.parse_args(argv)
    if a.cmd == "health":
        code, out = call(a.port, "GET", "/health")
    elif a.cmd == "notes-pending":
        code, out = call(a.port, "GET", "/v1/notes/pending")
    elif a.cmd == "edits":
        code, out = call(a.port, "POST", "/v1/edits", json.loads(a.json))
    elif a.cmd in ("readings", "readings-add"):
        code, out = call(a.port, "POST", "/v1/readings", {"url": a.url, "why": a.why})
    elif a.cmd == "collect":
        srcs = [s for s in a.sources.split(",") if s]
        code, out = call(a.port, "POST", "/v1/collect", {"sources": srcs} if srcs else {})
    else:
        code, out = call(a.port, "POST", f"/v1/notes/{a.id}/done", {"result": a.result})
    print(f"HTTP {code}")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if 200 <= code < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
