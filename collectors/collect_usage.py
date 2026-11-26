#!/usr/bin/env python3
"""用量看板采集（P2）：usage_data rows → usage.json（schema 见 README「usage.json」段 / 设计稿 §3.2，30 天滚动）。

正常模式：存快照 snapshot-usage.json（CACHE_DIR），build_home 把它写成 home-data 分支的 usage.json 一起提交推送。
--dry-run：只打印到 stdout，不写任何文件。
浪费规则：R-CRON-IDLE、R-RESUME-HEAVY、R-MAXTURNS、R-DENY；
R-REFETCH（同任务同 URL ≥2 次）、R-REREAD（同任务同文件整读 ≥3 次）读 worker transcript 的 tool_use（usage_transcripts.py，只取 url/path 计数）。
输出只有数字、日期、模型名和 task id，不含任何 prompt / 正文 / 会话标题。
"""
import argparse
import datetime as dt
import json
import sys
from collections import defaultdict

import usage_data
import usage_transcripts

DAYS = 30
TOP_N = 10
FAILED_IDS_MAX = 20
RESUME_HEAVY_CACHE = 200_000   # 单次 run 新写入缓存 ≥200k token（多为 resume 重灌整段上下文）
RESUME_HEAVY_TURNS = 10        # 且回合 ≤10：重灌上下文却只做了一点事
SOURCES = ("worker", "hermes_cron_heartbeat", "hermes_cron_other", "hermes_main", "hermes_local")


def _r(x, n=2):
    return round(x + 0.0, n)


def task_prefix(tid):
    if not tid:
        return None
    if tid.startswith("t-"):
        return "t-"
    return tid.split("-", 1)[0]


def day_summary(date, rows, scan=None):
    tot_cost = sum(r["cost_usd"] for r in rows)
    tot_tok = sum(r["tokens"] for r in rows)
    worker = [r for r in rows if r["source"] == "worker"]
    hermes_sess = [r for r in rows if r["source"].startswith("hermes_cron") or r["source"] == "hermes_local"]
    by_source = {}
    for s in SOURCES:
        rs = [r for r in rows if r["source"] == s]
        e = {"cost_usd": _r(sum(r["cost_usd"] for r in rs)), "tokens_m": _r(sum(r["tokens"] for r in rs) / 1e6, 1)}
        if s == "worker":
            e["runs"] = len({r["task_id"] for r in rs})
        elif s == "hermes_main":
            e["turns"] = sum(r["turns"] for r in rs)
        else:
            e["sessions"] = len(rs)
        by_source[s] = e
    by_model = defaultdict(lambda: {"cost_usd": 0.0, "tokens_m": 0.0})
    for r in rows:
        by_model[r["model"]]["cost_usd"] += r["cost_usd"]
        by_model[r["model"]]["tokens_m"] += r["tokens"] / 1e6
    by_model = {m: {"cost_usd": _r(v["cost_usd"]), "tokens_m": _r(v["tokens_m"], 1)}
                for m, v in sorted(by_model.items(), key=lambda kv: -kv[1]["cost_usd"])}
    by_prefix = defaultdict(float)
    for r in worker:
        by_prefix[task_prefix(r["task_id"])] += r["cost_usd"]
    # 按任务聚合（一个 run 可能多模型多行）
    tasks = {}
    for r in worker:
        t = tasks.setdefault(r["task_id"], {"task_id": r["task_id"], "model": r["model"], "cost_usd": 0.0, "turns": 0, "denials": 0,
                                            "template": r.get("template"), "subtype": r.get("subtype"), "exit_code": None,
                                            "cache_create": 0, "billing": r.get("billing"), "session_id": r.get("session_id")})
        t["cost_usd"] += r["cost_usd"]
        t["turns"] += r["turns"]
        t["denials"] += r["denials"]
        t["cache_create"] += r.get("cache_create", 0)
        if r.get("exit_code") is not None:
            t["exit_code"] = r["exit_code"]
    tl = sorted(tasks.values(), key=lambda t: -t["cost_usd"])
    top = [{"task_id": t["task_id"], "model": t["model"], "cost_usd": _r(t["cost_usd"]), "turns": t["turns"], "denials": t["denials"],
            "template": t["template"], "subtype": t["subtype"]} for t in tl[:TOP_N]]
    maxturns = [t for t in tl if t["subtype"] == "error_max_turns"]
    denied = [t for t in tl if t["denials"]]
    exit_bad = [t for t in tl if t["exit_code"] not in (None, 0)]
    failed_ids = ([{"task_id": t["task_id"], "kind": "error_max_turns"} for t in maxturns]
                  + [{"task_id": t["task_id"], "kind": "permission_denials", "count": t["denials"]} for t in denied]
                  + [{"task_id": t["task_id"], "kind": "exit_nonzero", "code": t["exit_code"]} for t in exit_bad])[:FAILED_IDS_MAX]
    return {
        "date": date,
        "total": {"cost_usd": _r(tot_cost), "tokens_m": _r(tot_tok / 1e6, 1), "runs": by_source["worker"]["runs"],
                  "sessions": len(hermes_sess)},
        "by_source": by_source,
        "by_model": by_model,
        "by_task_prefix": {k: _r(v) for k, v in sorted(by_prefix.items(), key=lambda kv: -kv[1]) if k},
        "turns": sum(r["turns"] for r in rows),
        "failures": {"error_max_turns": len(maxturns), "permission_denials": sum(t["denials"] for t in tl),
                     "exit_nonzero": len(exit_bad), "ids": failed_ids},
        "top_tasks": top,
        "waste": waste(rows, tl, scan),
        "billing_api_runs": sum(1 for t in tl if t["billing"] == "api"),
    }


def waste(rows, tasks, scan=None):
    """当天命中的浪费规则；count=0 的不列。scan(task) → {"refetch": n, "reread": n} | None（读 transcript；None 表示不跑这两条）。"""
    out = []
    idle = [r for r in rows if r["source"] == "hermes_cron_heartbeat" and r.get("idle")]
    if idle:
        out.append({"rule": "R-CRON-IDLE", "count": len(idle), "cost_usd": _r(sum(r["cost_usd"] for r in idle))})
    heavy = [t for t in tasks if t["cache_create"] >= RESUME_HEAVY_CACHE and 0 < t["turns"] <= RESUME_HEAVY_TURNS]
    if heavy:
        out.append({"rule": "R-RESUME-HEAVY", "count": len(heavy), "cost_usd": _r(sum(t["cost_usd"] for t in heavy)),
                    "ids": [t["task_id"] for t in heavy[:5]]})
    mt = [t for t in tasks if t["subtype"] == "error_max_turns"]
    if mt:
        out.append({"rule": "R-MAXTURNS", "count": len(mt), "cost_usd": _r(sum(t["cost_usd"] for t in mt)),
                    "ids": [t["task_id"] for t in mt[:5]]})
    dn = [t for t in tasks if t["denials"]]
    if dn:
        out.append({"rule": "R-DENY", "count": sum(t["denials"] for t in dn), "cost_usd": _r(sum(t["cost_usd"] for t in dn)),
                    "ids": [t["task_id"] for t in dn[:5]]})
    if scan:
        hits = {"R-REFETCH": [], "R-REREAD": []}
        for t in tasks:
            s = scan(t)
            if not s:
                continue
            if s.get("refetch"):
                hits["R-REFETCH"].append((t, s["refetch"]))
            if s.get("reread"):
                hits["R-REREAD"].append((t, s["reread"]))
        for rule, hs in hits.items():
            if hs:    # count = 多余的重复次数；没有 cost_usd（重复调用的成本无法从 run 总成本里拆出）
                out.append({"rule": rule, "count": sum(n for _, n in hs), "ids": [t["task_id"] for t, _ in hs[:5]]})
    return out


def week_summary(days_by_date, today):
    iso = today.isocalendar()
    mon = today - dt.timedelta(days=today.weekday())
    span = (today - mon).days
    this = [days_by_date.get((mon + dt.timedelta(days=i)).isoformat()) for i in range(span + 1)]
    last = [days_by_date.get((mon - dt.timedelta(days=7 - i)).isoformat()) for i in range(span + 1)]
    cost = lambda ds: sum(d["total"]["cost_usd"] for d in ds if d)
    fable = sum(v["cost_usd"] for d in this if d for m, v in d["by_model"].items() if "fable" in m)
    lc = cost(last)
    return {"iso": f"{iso[0]}-W{iso[1]:02d}", "cost_usd": _r(cost(this)),
            "vs_last_week_pct": round((cost(this) - lc) / lc * 100) if lc else None, "fable_cost_usd": _r(fable)}


def build(rows, today, now=None, days=DAYS, scan=None):
    now = now or dt.datetime.now(usage_data.TZ)
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(days)]
    day_objs = [day_summary(d, by_day.get(d, []), scan) for d in dates]   # 新的在前
    return {
        "schema": 1,
        "generated": now.replace(microsecond=0).isoformat(),
        "tz": "America/Los_Angeles",
        "quota": {"seven_day_pct": None, "resets_at": None, "five_hour_pct": None, "source": "none", "as_of": None},
        "days": day_objs,
        "week": week_summary({d["date"]: d for d in day_objs}, today),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="用量看板采集 → usage.json")
    ap.add_argument("--dry-run", action="store_true", help="打印 usage.json 到 stdout，不写文件")
    ap.add_argument("--day", help="--dry-run 时只打印这一天的对象（YYYY-MM-DD）")
    a = ap.parse_args(argv)
    now = dt.datetime.now(usage_data.TZ)
    today = now.date()
    since = (today - dt.timedelta(days=DAYS - 1)).isoformat()
    usage = build(usage_data.load_rows(since, today.isoformat()), today, now, scan=usage_transcripts.scan_task)
    if a.dry_run:
        out = usage
        if a.day:
            out = next((d for d in usage["days"] if d["date"] == a.day), None)
        json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 0
    from common import save_snapshot
    save_snapshot("usage", usage)
    d0 = usage["days"][0]
    print(f"usage ok: today ${d0['total']['cost_usd']} · {len(usage['days'])} days")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — 最后一行进 status.json 的 last_error
        print(f"usage FAIL: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        sys.exit(1)
