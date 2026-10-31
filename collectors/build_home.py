#!/usr/bin/env python3
"""汇总：本地快照 + readings.jsonl + status → home-data 分支的 home.json / status.json。

内容（去掉时间戳后）不变就不 commit；但距上次提交超过 HEARTBEAT_H 小时会提交一次心跳，
好让页面上的「数据更新于」不会因为数据恰好没变而显得过期。commit 后 push。
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys

import collect_gtasks
from collect_countdown import next_date
from life_ingest import load_life
from common import (CACHE_DIR, DATA_DIR, SOURCES, commit_and_push, data_lock, git, log, now_iso,
                    read_json, snapshot_path, sync_down, write_json)

SCHEMA = 1
HEARTBEAT_H = 3
FOLD_DAYS = 30
DUE_PAST_DAYS = 14
DUE_AHEAD_DAYS = 60
LEDGER = os.path.join(DATA_DIR, "readings.jsonl")


def load_readings():
    """→ (读物列表, 坏行数)。坏行（非 JSON / 非对象 / 缺 id）跳过并记日志，不拖垮整本账；读不出文件才抛。"""
    latest, bad = {}, 0
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    e = None
                if not isinstance(e, dict) or not e.get("id"):
                    bad += 1
                    log(f"build: readings.jsonl 第 {n} 行无效，跳过")
                    continue
                latest[e["id"]] = {**latest.get(e["id"], {}), **e}  # 同 id 后者生效
    out = []
    today = dt.date.today()
    for e in latest.values():
        if e.get("status") in ("done", "dropped"):
            continue
        e["rid"] = "r:" + e["id"].removeprefix("link-")   # 展示/复制用；账本 id 唯一故 rid 唯一
        try:
            e["folded"] = (today - dt.date.fromisoformat(e.get("added", "")[:10])).days > FOLD_DAYS
        except ValueError:
            e["folded"] = False
        out.append(e)
    return sorted(out, key=lambda x: x.get("ts", x.get("added", "")), reverse=True), bad


def slug(s):
    """标题 → ID 片段：去 emoji/标点，小写，空白→-，中文保留，≤30 字。"""
    s = re.sub(r"[^\w\s-]|_", "", s.lower())
    return re.sub(r"\s+", "-", s.strip())[:30] or "x"


def due_items(snaps, life=None, today=None):
    """倒数日 + Backlog 窗口 + life.jsonl（WP-08b）→ home["due"]。
    合并 → 去重（同 id 后者生效）→ 滤掉 [今天-14, 今天+60] 之外的有日期项 → 按 date 升序；无日期项（backlog）排最后。"""
    today = today or dt.date.today()
    lo, hi = today - dt.timedelta(days=DUE_PAST_DAYS), today + dt.timedelta(days=DUE_AHEAD_DAYS)
    items = []
    for c in ((snaps.get("countdown") or {}).get("data") or {}).get("items", []):
        d = next_date(c, today)
        if d:
            items.append({"id": f"d:countdown-{slug(c['label'])}", "title": c["label"], "date": d.isoformat(),
                          "note": "🔁 年度" if c.get("recur") else "", "source": "countdown",
                          **({"recur": True} if c.get("recur") else {})})
    b = (snaps.get("backlog") or {}).get("data") or {}
    for i in b.get("items", []):
        items.append({"id": f"d:backlog-{slug(i['title'])}", "title": i["title"], "date": i.get("date", ""),
                      "note": i.get("note", ""), "source": "backlog", **({"url": b["url"]} if b.get("url") else {})})
    items.extend(life or [])
    latest = {}
    for it in items:
        latest[it["id"]] = {**latest.get(it["id"], {}), **it}
    kept = [it for it in latest.values() if not it.get("date") or lo.isoformat() <= it["date"] <= hi.isoformat()]
    return sorted(kept, key=lambda it: (not it.get("date"), it.get("date", "")))


def edit_counts():
    """edits.jsonl（编辑流账本，同 id 后者生效）→ {target: {"pending": 未落卡, "hermes": 附言待 Hermes}}。坏行跳过。"""
    latest = {}
    try:
        with open(os.path.join(DATA_DIR, "edits.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("id"):
                    latest[e["id"]] = {**latest.get(e["id"], {}), **e}
    except OSError:
        return {}
    out = {}
    for e in latest.values():
        key = {"pending": "pending", "needs_hermes": "hermes"}.get(e.get("state"))
        if key and e.get("target"):
            out.setdefault(e["target"], {"pending": 0, "hermes": 0})[key] += 1
    return out


def normalized(home, status):
    h = json.loads(json.dumps(home))
    h.pop("generated", None)
    for s in h.get("sources", {}).values():
        s.pop("updated", None)
    if isinstance(h.get("todos"), dict):
        h["todos"].pop("synced_at", None)   # 同 sources.updated：只有时间戳变不算内容变
    st = {k: {kk: vv for kk, vv in v.items() if kk != "last_ok"} for k, v in (status or {}).items()}
    return json.dumps([h, st], sort_keys=True, ensure_ascii=False)


def committed(path):
    r = git("show", f"HEAD:{path}", check=False)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def source_entry(s, st, snap, readings_err):
    updated = snap["fetched"] if snap else None
    if s == "readings":
        updated = now_iso() if not readings_err else None
    fails = st.get("consecutive_failures", 0)
    ok = fails == 0 and not (s == "readings" and readings_err)
    if ok:
        err = ""
    elif s == "readings" and readings_err:
        err = readings_err
    else:
        err = st.get("last_error_msg", "")
    return {"updated": updated, "ok": ok, "consecutive_failures": fails, "last_error": err}


def main():
    status = read_json(os.path.join(CACHE_DIR, "status.json"), {}) or {}
    with data_lock():
        sync_down()
        snaps = {s: read_json(snapshot_path(s)) for s in ("menxia", "dashboard", "countdown", "backlog", "gtasks")}
        old_home, old_status = committed("home.json"), committed("status.json")
        readings, readings_err = [], ""
        try:
            readings, bad = load_readings()
            if bad:
                readings_err = f"readings.jsonl 有 {bad} 行无效，已跳过"
        except Exception as e:  # noqa: BLE001
            readings_err = str(e)[:200]
            readings = (old_home or {}).get("readings", [])  # 读不出账本：沿用上一份，不写空列表
            log(f"build: readings.jsonl 读取失败，沿用上一份 {len(readings)} 条：{readings_err}")
        life, life_updated = [], None
        try:
            life, life_updated = load_life()
        except Exception as e:  # noqa: BLE001
            log(f"build: life.jsonl 读取失败：{str(e)[:200]}")
        sources = {}
        for s in SOURCES:
            st = status.get(s, {})
            snap = snaps.get(s)
            sources[s] = source_entry(s, st, snap, readings_err)
        sources["life"]["updated"] = life_updated
        m = (snaps.get("menxia") or {}).get("data") or {}
        d = (snaps.get("dashboard") or {}).get("data") or {}
        ec = edit_counts()
        for p in d.get("cards", []):
            c = ec.get(p.get("id"), {})
            p["pending_edits"] = c.get("pending", 0)
            p["hermes_edits"] = c.get("hermes", 0)
        home = {
            "schema": SCHEMA,
            "generated": now_iso(),
            "sources": sources,
            "folders": {"total": m.get("total", 0), "counts": m.get("counts", {}), "oldest": m.get("oldest", {})},
            "library": m.get("library", []),
            "projects": d.get("cards", []),
            "readings": readings,
            "due": due_items(snaps, life),
        }
        # Google Tasks「今日待办」：独立于 due[]；从没采过就不写这个键（客户端据此不显示该栏）
        todos = collect_gtasks.from_snapshot(snaps.get("gtasks"), status.get("gtasks"), collect_gtasks.la_today())
        if todos is not None:
            home["todos"] = todos
        changed = old_home is None or normalized(home, status) != normalized(old_home, old_status)
        stale = True
        if old_home and old_home.get("generated"):
            gen = dt.datetime.fromisoformat(old_home["generated"].replace("Z", "+00:00"))
            stale = dt.datetime.now(dt.timezone.utc) - gen > dt.timedelta(hours=HEARTBEAT_H)
        if not changed and not stale:
            log("build: 内容未变，不提交")
            return 0 if not readings_err else 3
        write_json(os.path.join(DATA_DIR, "home.json"), home)
        write_json(os.path.join(DATA_DIR, "status.json"), status)
        msg = "home: update" if changed else "home: heartbeat"
        pushed = commit_and_push(["home.json", "status.json"], msg)
        log(f"build: {msg} pushed={pushed}")
    return 0 if not readings_err else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.TimeoutExpired as e:
        print(f"build FAIL: timeout {e}", file=sys.stderr)
        sys.exit(1)
