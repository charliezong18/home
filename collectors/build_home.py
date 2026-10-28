#!/usr/bin/env python3
"""汇总：本地快照 + readings.jsonl + status → home-data 分支的 home.json / status.json。

内容（去掉时间戳后）不变就不 commit；但距上次提交超过 HEARTBEAT_H 小时会提交一次心跳，
好让页面上的「数据更新于」不会因为数据恰好没变而显得过期。commit 后 push。
"""
import datetime as dt
import json
import os
import subprocess
import sys

from common import (CACHE_DIR, DATA_DIR, SOURCES, commit_and_push, data_lock, git, log, now_iso,
                    read_json, snapshot_path, sync_down, write_json)

SCHEMA = 1
HEARTBEAT_H = 3
FOLD_DAYS = 30
LEDGER = os.path.join(DATA_DIR, "readings.jsonl")


def load_readings():
    latest = {}
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)  # 坏行直接抛，交给 run_all 记 readings 源失败
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
    return sorted(out, key=lambda x: x.get("ts", x.get("added", "")), reverse=True)


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
        snaps = {s: read_json(snapshot_path(s)) for s in ("menxia", "dashboard")}
        readings, readings_err = [], ""
        try:
            readings = load_readings()
        except Exception as e:  # noqa: BLE001
            readings_err = str(e)[:200]
        sources = {}
        for s in SOURCES:
            st = status.get(s, {})
            snap = snaps.get(s)
            sources[s] = source_entry(s, st, snap, readings_err)
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
        }
        old_home, old_status = committed("home.json"), committed("status.json")
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
