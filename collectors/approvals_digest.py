#!/usr/bin/env python3
"""门下画可执行台账 → 30 分钟汇报。只打印，不发送（Hermes cron 空输出即静默）。

只报执行器（history.by == "autoexec"）的「开始执行 / 完成 / 卡住」；Hermes 手工推进的不算。
游标：$STATE_DIR/digest.json 记每个 key 已汇报到的 history 条数。
先打印、后推进游标；输出为空不推进。打印本身失败（进程被杀、stdout 断）→ 游标不动，下次重报（宁可重复，不漏报）。
首次运行（无游标）只报 autoexec_since 之后的变化（未配置则不输出）。
用法：approvals_digest.py [--config PATH] [--peek]   --peek：打印但不推进游标
"""
import argparse
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
        what = f"已接单 {job.get('task_id') or ''}".strip()
    elif h["to"] == "done":
        what = o.get("summary") or h.get("evidence") or "已完成"
    else:
        what = o.get("summary") or h.get("reason") or "阻塞"
        if h.get("reason") and h["reason"][:60] not in what:
            what += f"（{h['reason'][:120]}）"
    rep = f"\n  报告：{a['report']}" if a.get("report") and h["to"] != "executing" else ""
    return f"- {VERB[h['to']]}：{title}——{what}\n  {R.link(job)}{rep}"


def digest(cfg):
    """返回 (文本, 新游标)。不写任何文件。"""
    cfg = cfg if "_since" in cfg else R.normalize_config(cfg)
    jobs = R.read_ledger().get("jobs", {})
    cur = R.load_json(cursor_path())
    since = cfg["_since"]
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
        lines += [line(j, h) for h in fresh if h.get("to") in VERB and h.get("by") == "autoexec"]
    warn = "".join(f"\n（配置告警：{w}）" for w in cfg["_warnings"])
    return (("门下自动执行汇报：\n" + "\n".join(lines) + warn) if lines else ""), new_cur


def commit(new_cur):
    os.makedirs(A.STATE_DIR, exist_ok=True)
    R.save_json(cursor_path(), new_cur)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config")
    ap.add_argument("--peek", action="store_true")
    a = ap.parse_args(argv)
    out, new_cur = digest(R.load_config(a.config))
    if out:
        print(out, flush=True)
        if not a.peek:
            commit(new_cur)
    return 0


if __name__ == "__main__":
    sys.exit(main())
