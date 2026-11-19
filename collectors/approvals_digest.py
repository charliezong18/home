#!/usr/bin/env python3
"""门下画可执行台账 → 30 分钟汇报。只打印，不发送（Hermes cron 空输出即静默）。

游标：$STATE_DIR/digest.json 记每个 key 已汇报到的 history 条数；只报之后新增的
「开始执行 / 完成 / 卡住」。首次运行（无游标）只报 autoexec_since 之后的变化（未配置则只建游标、不输出）。
用法：approvals_digest.py [--config PATH] [--peek]   --peek：打印但不前移游标
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approvals as A  # noqa: E402
import approval_runner as R  # noqa: E402

VERB = {"executing": "开始执行", "done": "完成", "blocked": "卡住"}


def cursor_path():
    return os.path.join(A.STATE_DIR, "digest.json")


def line(job, h):
    a = job.get("autoexec") or {}
    o = a.get("outcome") or {}
    title = job.get("title") or job["key"]
    if h["to"] == "executing":
        what = f"已派工 {job.get('task_id') or ''}".strip()
    elif h["to"] == "done":
        what = o.get("summary") or h.get("evidence") or "已完成"
    else:
        what = o.get("summary") or h.get("reason") or "阻塞"
        if h.get("reason") and h["reason"] not in what:
            what += f"（{h['reason'][:120]}）"
    rep = f"\n  报告：{a['report']}" if a.get("report") and h["to"] != "executing" else ""
    return f"- {VERB[h['to']]}：{title}——{what}\n  {R.link(job)}{rep}"


def digest(cfg, advance=True):
    jobs = R.read_ledger().get("jobs", {})
    cur = R.load_json(cursor_path())
    since = A.parse(cfg["autoexec_since"]) if cfg.get("autoexec_since") else None
    lines, new_cur = [], {}
    for k, j in sorted(jobs.items(), key=lambda kv: kv[1]["history"][-1]["at"]):
        hist = j.get("history") or []
        new_cur[k] = len(hist)
        if cur is not None:
            fresh = hist[cur.get(k, 0):]
        elif since:
            fresh = [h for h in hist if A.parse(h["at"]) >= since]
        else:
            fresh = []
        lines += [line(j, h) for h in fresh if h.get("to") in VERB]
    if advance:
        os.makedirs(A.STATE_DIR, exist_ok=True)
        R.save_json(cursor_path(), new_cur)
    return ("门下自动执行汇报：\n" + "\n".join(lines)) if lines else ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config")
    ap.add_argument("--peek", action="store_true")
    a = ap.parse_args(argv)
    out = digest(R.load_config(a.config), advance=not a.peek)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
