#!/usr/bin/env python3
"""life.jsonl 账本写入口（Claude cron 会话唯一允许的写法）。

  life_ingest.py add --json '<对象或数组>' [--no-git]
  life_ingest.py done --id d:cal-xxxx [--no-git]
  life_ingest.py list [--window 14]

账本 append-only，同 id 后者生效。不存金额、邮件正文、发件人邮箱：title/note 里出现金额或 @ 直接拒绝（整批不写）。
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

from common import DATA_DIR, commit_and_push, data_lock, die, now_iso, sync_down

ID_RE = re.compile(r"^d:(cal|bill|parcel)-[A-Za-z0-9_-]+$")
SOURCES_OK = ("calendar", "gmail")
FIELDS = ("title", "date", "note", "source", "status")
MONEY_RE = re.compile(r"[$¥￥€£]\s*\d|\d\s*(?:美元|元|USD|CNY|RMB)", re.I)
TITLE_MAX = 60
NOTE_MAX = 120


def ledger_path():
    return os.path.join(DATA_DIR, "life.jsonl")


def read_rows():
    rows = []
    try:
        with open(ledger_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("id"):
                    rows.append(e)
    except OSError:
        pass
    return rows


def merged(rows=None):
    latest = {}
    for e in read_rows() if rows is None else rows:
        latest[e["id"]] = {**latest.get(e["id"], {}), **e}
    return latest


def load_life():
    """给 build_home 用：归并后 status==open 的条目 + 账本最后一行 ts（无账本为 None）。"""
    rows = read_rows()
    items = [{k: e[k] for k in ("id", "title", "date", "note", "source") if k in e}
             for e in merged(rows).values() if e.get("status", "open") == "open"]
    return items, (rows[-1].get("ts") if rows else None)


def validate(e):
    """返回规整后的条目；不合格抛 ValueError（消息给 cron 会话看）。"""
    if not isinstance(e, dict):
        raise ValueError("条目必须是对象")
    if not ID_RE.match(str(e.get("id", ""))):
        raise ValueError(f"id 非法：{e.get('id')!r}（应为 d:cal-/d:bill-/d:parcel- 开头）")
    title, note = str(e.get("title", "")).strip(), str(e.get("note", "")).strip()
    if not title or len(title) > TITLE_MAX:
        raise ValueError(f"title 为空或超过 {TITLE_MAX} 字：{e['id']}")
    if len(note) > NOTE_MAX:
        raise ValueError(f"note 超过 {NOTE_MAX} 字：{e['id']}")
    for name, text in (("title", title), ("note", note)):
        if MONEY_RE.search(text):
            raise ValueError(f"{name} 含金额，拒绝写入（账单不存金额）：{e['id']}")
        if "@" in text:
            raise ValueError(f"{name} 含邮箱/@，拒绝写入：{e['id']}")
    try:
        dt.date.fromisoformat(str(e.get("date", "")))
    except ValueError:
        raise ValueError(f"date 须为 YYYY-MM-DD：{e['id']}")
    if e.get("source") not in SOURCES_OK:
        raise ValueError(f"source 须为 calendar|gmail：{e['id']}")
    status = e.get("status", "open")
    if status not in ("open", "done"):
        raise ValueError(f"status 须为 open|done：{e['id']}")
    return {"id": e["id"], "title": title, "date": e["date"], "note": note, "source": e["source"], "status": status}


def append(entries, no_git, msg):
    """去重后追加；返回 (写入数, 跳过数)。"""
    def go():
        cur = merged()
        new, skipped = [], 0
        for e in entries:
            old = cur.get(e["id"])
            if old and all(old.get(k) == e[k] for k in FIELDS if k in e):
                skipped += 1
                continue
            row = {**e, "ts": now_iso(), "from": "claude-cron"}
            new.append(row)
            cur[e["id"]] = {**old, **row} if old else row
        if new:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(ledger_path(), "a", encoding="utf-8") as f:
                for row in new:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return new, skipped

    if no_git:
        new, skipped = go()
    else:
        with data_lock():
            sync_down()
            new, skipped = go()
            if new:
                commit_and_push(["life.jsonl"], f"{msg}{len(new)}")
    return len(new), skipped


def cmd_add(a):
    try:
        raw = json.loads(a.json)
        entries = [validate(e) for e in (raw if isinstance(raw, list) else [raw])]
    except ValueError as ex:
        die(f"life: 拒绝写入：{ex}", 2)
    n, skipped = append(entries, a.no_git, "life: +")
    print(f"life: +{n} skipped={skipped}")


def cmd_done(a):
    if not ID_RE.match(a.id):
        die(f"life: id 非法：{a.id}", 2)
    if a.id not in merged():
        die(f"life: 账本里没有 {a.id}", 2)
    cur = merged()[a.id]
    n, _ = append([validate({**cur, "status": "done"})], a.no_git, "life: done +")
    print(f"life: done {a.id}" if n else f"life: {a.id} 已是 done")


def cmd_list(a):
    today = dt.date.today()
    hi = (today + dt.timedelta(days=a.window)).isoformat()
    rows = sorted((e for e in merged().values() if e.get("status", "open") == "open"
                   and today.isoformat() <= e.get("date", "") <= hi), key=lambda e: e["date"])
    for e in rows:
        print(f"{e['date']}  {e['id']}  {e['title']}  [{e.get('source', '')}]")
    print(f"life: {len(rows)} open within {a.window}d")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("--json", required=True)
    s.add_argument("--no-git", action="store_true", help="只写文件不碰 git（测试用）")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("done")
    s.add_argument("--id", required=True)
    s.add_argument("--no-git", action="store_true")
    s.set_defaults(fn=cmd_done)
    s = sub.add_parser("list")
    s.add_argument("--window", type=int, default=14)
    s.set_defaults(fn=cmd_list)
    a = p.parse_args(argv)
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
