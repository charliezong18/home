#!/usr/bin/env python3
"""dibao 命令行（给 Hermes / Claude 派工用，urllib，无依赖）。

  cli.py [--port 8766] health
  cli.py edits --json '{"target":"p:x","fields":{"next":"…"},"from":"claude"}'
  cli.py readings-add --url URL [--why 理由]      （readings 为旧别名）
  cli.py notes-pending
  cli.py note-done --id ID [--result 文本]
  cli.py collect [--sources menxia,dashboard]     立即触发一轮采集，返回 job id（GET /v1/collect/{job} 查）
  cli.py push-test [--title T] [--body B] [--url menxia://home]   手动发一条推送（APNs，无设备则 ntfy）；会真发，验收用
  cli.py todo-done <task_id> [--title T]          把一条 Google Tasks 标完成（会真改 Google Tasks）
  cli.py todo-undo <task_id> [--title T]          撤销完成（标回未完成）
  cli.py digest morning|evening [--dry-run]       早 / 晚报：本地读 home.json 打印将推内容，不发、不连服务（缺省即 dry-run）
  cli.py digest morning|evening --send            立刻经服务发一条（会真发；今天已发过返回 dup，不重发）
输出响应 JSON；HTTP 非 2xx 退出码 1（todo 执行失败 = HTTP 502）。连不上 / 超时输出 {"ok": false, "error": …}，退出码 1。
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 60


def call(port, method, path, body=None):
    """返回 (HTTP 状态码, JSON)；连接失败 / 超时返回 (None, {"ok": False, "error": …})。"""
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")
    except (urllib.error.URLError, OSError) as e:      # 连接被拒（服务没起）、超时
        reason = getattr(e, "reason", None) or e
        return None, {"ok": False, "error": f"{type(reason).__name__}: {reason}"[:300]}


def digest_dry_run(kind, now=None, out=print, digest_cfg=None):
    """本地组装（与服务同一套 digest.build + 本机 config.toml [digest]），不发送、不连服务、不写任何文件。
    digest_cfg 可注入（测试用）；None = 读本机配置。"""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (here, os.path.join(os.path.dirname(here), "collectors")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import digest
    import events
    import todo_actions
    from common import DATA_DIR, read_json
    if digest_cfg is None:
        import config
        digest_cfg = config.load().digest
    digest.configure(digest_cfg)
    day =digest.now_la(now).date().isoformat()
    done = digest.todos_done_today(todo_actions.read_records(os.path.join(DATA_DIR, "todo-actions.jsonl")), day)
    d = digest.build(kind, read_json(os.path.join(DATA_DIR, "home.json"), {}) or {}, day, done)
    sent = False
    try:
        with open(events.state_path(), encoding="utf-8") as f:
            sent = any(json.loads(line).get("key") == d["key"] for line in f if line.strip().startswith("{"))
    except (OSError, ValueError):
        pass
    out(f"[dry-run] {kind} {day}  key={d['key']}  今天已发过={'是' if sent else '否'}  url={d['url']}")
    out(f"标题：{d['title']}")
    out("正文：\n" + d["body"])
    return 0


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
    p = sub.add_parser("push-test")
    p.add_argument("--title", default="测试")
    p.add_argument("--body", default="门下推送")
    p.add_argument("--url", default="menxia://home")
    for name in ("todo-done", "todo-undo"):
        p = sub.add_parser(name)
        p.add_argument("task_id")
        p.add_argument("--title", default="")
    p = sub.add_parser("digest")
    p.add_argument("kind", choices=["morning", "evening"])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="只打印将推内容（缺省）")
    g.add_argument("--send", action="store_true", help="经服务真发")
    a = ap.parse_args(argv)
    if a.cmd == "digest" and not a.send:
        return digest_dry_run(a.kind)
    if a.cmd == "digest":
        code, out = call(a.port, "POST", f"/v1/digest/{a.kind}", {})
    elif a.cmd == "health":
        code, out = call(a.port, "GET", "/health")
    elif a.cmd == "notes-pending":
        code, out = call(a.port, "GET", "/v1/notes/pending")
    elif a.cmd == "edits":
        code, out = call(a.port, "POST", "/v1/edits", json.loads(a.json))
    elif a.cmd in ("readings", "readings-add"):
        code, out = call(a.port, "POST", "/v1/readings", {"url": a.url, "why": a.why})
    elif a.cmd == "push-test":
        code, out = call(a.port, "POST", "/v1/push/test", {"title": a.title, "body": a.body, "url": a.url})
    elif a.cmd == "collect":
        srcs = [s for s in a.sources.split(",") if s]
        code, out = call(a.port, "POST", "/v1/collect", {"sources": srcs} if srcs else {})
    elif a.cmd in ("todo-done", "todo-undo"):
        op = "complete" if a.cmd == "todo-done" else "undo"
        tid = urllib.parse.quote(a.task_id, safe="")
        code, out = call(a.port, "POST", f"/v1/todos/{tid}/{op}", {"title": a.title, "from": "claude"})
    else:
        code, out = call(a.port, "POST", f"/v1/notes/{a.id}/done", {"result": a.result})
    print(f"HTTP {code}" if code else "HTTP -（未连上 dibao）")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if code and 200 <= code < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
