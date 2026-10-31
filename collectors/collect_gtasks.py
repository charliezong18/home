#!/usr/bin/env python3
"""Google Tasks 采集器：本机已授权的 `gog` CLI（只读）→ 快照 gtasks → build_home 归成 home.json 的 `todos`。

只调 `gog tasks list`（分页），不创建 / 修改 / 完成任何任务。
快照只存未完成任务的 id / title / due，**不存 notes**（隐私）。
「今天到期 / 已过期 / 无截止日」在 build 时按 America/Los_Angeles 的今天现算（collect_gtasks.todos），
所以采集失败几轮时分段也不会停在旧的一天；失败时不写快照，build 沿用上一份并在 todos.error 带上错误。

Google Tasks 的 due 只有日期有效（API 存成 `YYYY-MM-DDT00:00:00.000Z`，时刻部分被丢弃），
所以取前 10 位当日期，**不做时区换算**（换到洛杉矶会整体提前一天）；只有「今天」按洛杉矶算。

  collect_gtasks.py            采集 → 快照
  collect_gtasks.py --dry-run  只读演练：打印三段计数，不写快照、不打印标题
"""
import datetime as dt
import json
import os
import subprocess
import sys

from common import die, log, now_iso, save_snapshot

# 公共仓不写账号相关的真实列表 ID：@default = Tasks API 的默认列表（My Tasks）；要换列表用环境变量
LIST_ID = os.environ.get("AI_HOME_GTASKS_LIST", "@default")
LIST_NAME = "My Tasks"
TZ = "America/Los_Angeles"
GOG = "gog"
PAGE_SIZE = 100          # gog --max 上限
MAX_PAGES = 20           # 2000 条封顶，防分页死循环
CALL_TIMEOUT = 30        # 单次 gog 子进程；dibao 给整个采集器 120 s
TITLE_MAX = 80
NODATE_MAX = 20


def la_today(now=None):
    """洛杉矶的今天。now 可传带时区的 datetime（测试用）。"""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(TZ)
    return (now.astimezone(tz) if now else dt.datetime.now(tz)).date()


def _page_items(obj):
    """gog --json 一页 → (任务列表, nextPageToken)。容忍信封（{"tasks"|"items": [...], "nextPageToken"}）或裸数组。"""
    if isinstance(obj, list):
        return obj, None
    if not isinstance(obj, dict):
        raise ValueError(f"gog 输出不是 JSON 对象/数组：{type(obj).__name__}")
    items = None
    for k in ("tasks", "items"):
        if isinstance(obj.get(k), list):
            items = obj[k]
            break
    if items is None:   # 别的键名：取第一个列表值；一个都没有 = 空页
        items = next((v for v in obj.values() if isinstance(v, list)), [])
    return items, obj.get("nextPageToken") or None


def fetch(runner=subprocess.run, list_id=LIST_ID):
    """分页拉全量，返回原始任务 dict 列表（gog 默认输出含已完成，由 open_tasks 滤掉）。失败抛 RuntimeError。"""
    out, token = [], None
    for _ in range(MAX_PAGES):
        cmd = [GOG, "tasks", "list", list_id, "--json", "--no-input", "--color=never", f"--max={PAGE_SIZE}"]
        if token:
            cmd.append(f"--page={token}")
        try:
            r = runner(cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"gog 超时 {CALL_TIMEOUT}s") from None
        except OSError as e:
            raise RuntimeError(f"gog 起不来：{e}") from None
        if r.returncode != 0:
            raise RuntimeError(f"gog rc={r.returncode}: {(r.stderr or '').strip()[:200]}")
        try:
            items, token = _page_items(json.loads(r.stdout or "null"))
        except ValueError as e:
            raise RuntimeError(f"gog 输出解析失败：{str(e)[:200]}") from None
        out.extend(i for i in items if isinstance(i, dict))
        if not token:
            return out
    raise RuntimeError(f"分页超过 {MAX_PAGES} 页，停止")


def open_tasks(raw):
    """原始任务 → 未完成的 [{id, title, due}]（due = YYYY-MM-DD 或 None）。丢掉 notes 等一切其它字段。"""
    out = []
    for t in raw:
        if not t.get("id") or t.get("status") == "completed" or t.get("completed") or t.get("deleted") or t.get("hidden"):
            continue
        due = (t.get("due") or "")[:10]
        try:
            due = dt.date.fromisoformat(due).isoformat()
        except ValueError:
            due = None
        out.append({"id": str(t["id"]), "title": (t.get("title") or "").strip(), "due": due})
    return out


def todos(tasks, today, synced_at=None, error=None):
    """未完成任务 + 洛杉矶今天 → home.json 的 todos（契约见 PR 正文；未来到期的不进任何一段）。"""
    def item(t):
        return {"id": t["id"], "title": (t.get("title") or "（无标题）")[:TITLE_MAX], "due": t.get("due")}
    iso = today.isoformat()
    today_l = [item(t) for t in tasks if t.get("due") == iso]
    overdue = sorted((item(t) for t in tasks if t.get("due") and t["due"] < iso), key=lambda x: x["due"])
    nodate = [item(t) for t in tasks if not t.get("due")]
    return {
        "source": "google-tasks", "list": LIST_NAME, "synced_at": synced_at, "error": error,
        "today": today_l, "overdue": overdue, "nodate": nodate[:NODATE_MAX],
        "counts": {"today": len(today_l), "overdue": len(overdue), "nodate": len(nodate)},
    }


def from_snapshot(snap, st, today):
    """build_home 用：快照 + status.json 里的 gtasks 段 → todos；从没跑过 → None（客户端不显示该栏）。"""
    st = st or {}
    failing = int(st.get("consecutive_failures") or 0) > 0
    error = (st.get("last_error_msg") or "采集失败")[:200] if failing else None
    if not snap and not failing:
        return None
    data = (snap or {}).get("data") or {}
    return todos(data.get("tasks") or [], today, synced_at=(snap or {}).get("fetched"), error=error)


def main(argv=None, runner=subprocess.run):
    argv = sys.argv[1:] if argv is None else argv
    try:
        tasks = open_tasks(fetch(runner))
    except RuntimeError as e:
        die(f"gtasks FAIL: {e}")
    if "--dry-run" in argv:
        c = todos(tasks, la_today(), synced_at=now_iso())["counts"]
        print(f"gtasks dry-run: open={len(tasks)} today={c['today']} overdue={c['overdue']} nodate={c['nodate']}")
        return 0
    save_snapshot("gtasks", {"list": LIST_NAME, "list_id": LIST_ID, "tasks": tasks})
    log(f"gtasks ok: open={len(tasks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
