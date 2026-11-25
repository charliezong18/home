#!/usr/bin/env python3
"""用量共享数据层（P1）：worker runs + Hermes state.db → 统一 rows。纯标准库，只读。

隐私硬约束：rows 里只有数字、日期、模型名和 id。绝不读出 prompt / question / result / 会话标题正文：
- runs/*.json 只取 usage / modelUsage / total_cost_usd / num_turns / permission_denials / subtype（不取 result）
- ledger.json 只取 template / model / created_at / finished_at（不取 question）
- state.db 的会话标题只在 SQL 里做「是否含巡检 / 是否以某 cron job 名开头」判断；
  [SILENT] 判断也在 SQL 里做（instr），content 不进 Python。

row 字段：day（America/Los_Angeles）、source（worker | hermes_cron_heartbeat | hermes_cron_other |
hermes_main | hermes_local）、model、task_id、template、cost_usd、tokens、turns、denials、subtype、
exit_code、billing、idle（heartbeat 回 [SILENT]）、cache_create。

用法：python3 usage_data.py [--day YYYY-MM-DD] [--days 30] [--json]
"""
import argparse
import datetime as dt
import glob
import json
import os
import sqlite3
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
HOME = os.path.expanduser("~")
DISPATCH = os.environ.get("USAGE_DISPATCH_DATA", os.path.join(HOME, "Developer/op/hermes-dispatch/data"))
STATE_DB = os.environ.get("USAGE_HERMES_DB", os.path.join(HOME, ".hermes/state.db"))
CRON_JOBS = os.environ.get("USAGE_CRON_JOBS", os.path.join(HOME, ".hermes/cron/jobs.json"))
HEARTBEAT_JOB_ID = "e8319478c051"
HEARTBEAT_MARK = "巡检"
SILENT = "[SILENT]"
MAIN_SOURCES = ("telegram", "desktop")      # 长会话：按 assistant 消息数切日
LOCAL_SOURCES = ("local", "oneshot")


def la_day(ts):
    """epoch 秒 / ISO 串 → 洛杉矶日期串；解析不了返回 None。"""
    try:
        if isinstance(ts, (int, float)):
            d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        else:
            d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(TZ).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def norm_model(m):
    """claude-opus-5-5 / opus / claude-opus-5-5[1m] → opus-5-5 / opus（只做去前缀和括注，不猜版本）。"""
    m = str(m or "unknown").lower().split("[")[0].strip()
    return m.removeprefix("claude-") or "unknown"


def _num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def load_ledger(dispatch=DISPATCH):
    """ledger.json → {task_id: {template, model, created_at, finished_at}}（只取元数据，不取 question）。"""
    try:
        with open(os.path.join(dispatch, "ledger.json"), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    tasks = raw.get("tasks", raw) if isinstance(raw, dict) else {}
    out = {}
    if isinstance(tasks, dict):
        for tid, t in tasks.items():
            if isinstance(t, dict):
                out[tid] = {k: t.get(k) for k in ("template", "model", "created_at", "finished_at")}
    return out


def worker_rows(dispatch=DISPATCH, since=None, ledger=None):
    """runs/<task>.json → 每个 run 每个模型一行（turns/denials/exit 只记在该 run 的第一行，避免重复计）。
    since = 最早日期串；文件 mtime 早于 since 前一天的直接跳过（省 IO）。"""
    ledger = load_ledger(dispatch) if ledger is None else ledger
    rows = []
    cutoff = None
    if since:
        cutoff = dt.datetime.fromisoformat(since).replace(tzinfo=TZ).timestamp() - 86400
    for path in sorted(glob.glob(os.path.join(dispatch, "runs", "*.json"))):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if cutoff is not None and mtime < cutoff:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError):
            continue                                    # 还在写的 / 空文件
        if not isinstance(j, dict) or "total_cost_usd" not in j:
            continue
        stem = os.path.basename(path)[:-5]
        meta = ledger.get(stem, {})
        day = la_day(meta.get("finished_at")) or la_day(mtime)
        if since and (not day or day < since):
            continue
        exit_txt = _read_text(os.path.join(dispatch, "runs", stem + ".exit"))
        try:
            exit_code = int(exit_txt) if exit_txt not in (None, "") else None
        except ValueError:
            exit_code = None
        common = {"day": day, "source": "worker", "task_id": stem, "template": meta.get("template"),
                  "subtype": j.get("subtype"), "billing": _read_text(os.path.join(dispatch, "runs", stem + ".billing")) or "plan"}
        first = {"turns": int(_num(j.get("num_turns"))), "denials": len(j.get("permission_denials") or []), "exit_code": exit_code}
        zero = {"turns": 0, "denials": 0, "exit_code": None}
        mu = j.get("modelUsage") if isinstance(j.get("modelUsage"), dict) else {}
        parts = []
        for m, u in mu.items():
            if not isinstance(u, dict):
                continue
            tok = sum(int(_num(u.get(k))) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
            parts.append((norm_model(m), _num(u.get("costUSD")), tok, int(_num(u.get("cacheCreationInputTokens")))))
        if not parts:                                   # 没有 modelUsage：用顶层 usage + 账本模型
            u = j.get("usage") if isinstance(j.get("usage"), dict) else {}
            tok = sum(int(_num(u.get(k))) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            parts = [(norm_model(meta.get("model")), _num(j.get("total_cost_usd")), tok, int(_num(u.get("cache_creation_input_tokens"))))]
        parts.sort(key=lambda p: -p[1])
        for i, (m, cost, tok, cc) in enumerate(parts):
            rows.append({**common, **(first if i == 0 else zero), "model": m, "cost_usd": round(cost, 6),
                         "tokens": tok, "cache_create": cc, "idle": False})
    return rows


def heartbeat_names(jobs_path=CRON_JOBS):
    """jobs.json 里 heartbeat job 的 name（会话标题以它开头）；读不到返回 []。"""
    try:
        with open(jobs_path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    return [j["name"] for j in jobs if isinstance(j, dict) and j.get("id") == HEARTBEAT_JOB_ID and j.get("name")]


def _connect_ro(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)


def hermes_rows(db_path=STATE_DB, since=None, jobs_path=CRON_JOBS, until=None):
    """state.db → rows。cron/local/oneshot 按 started_at 切日；telegram/desktop 长会话按当天 assistant 消息数占比摊成本。"""
    if not os.path.exists(db_path):
        return []
    lo = dt.datetime.fromisoformat(since).replace(tzinfo=TZ).timestamp() if since else 0
    hi = (dt.datetime.fromisoformat(until).replace(tzinfo=TZ) + dt.timedelta(days=1)).timestamp() if until else 1e12
    names = heartbeat_names(jobs_path)
    # 标题判断全在 SQL 里：is_hb 只回 0/1，标题本身不出数据库
    hb_sql = "(instr(coalesce(s.title,''), ?) > 0" + "".join(" OR substr(coalesce(s.title,''), 1, ?) = ?" for _ in names) + ")"
    hb_args = [HEARTBEAT_MARK] + [x for n in names for x in (len(n), n)]
    tok = "coalesce(s.input_tokens,0)+coalesce(s.output_tokens,0)+coalesce(s.cache_read_tokens,0)+coalesce(s.cache_write_tokens,0)"
    rows = []
    con = _connect_ro(db_path)
    try:
        q = (f"SELECT s.source, s.model, s.started_at, coalesce(s.estimated_cost_usd,0), {tok}, coalesce(s.cache_write_tokens,0), "
             f"{hb_sql}, EXISTS(SELECT 1 FROM messages m WHERE m.session_id = s.id AND m.role = 'assistant' AND instr(coalesce(m.content,''), ?) > 0), "
             "coalesce(s.api_call_count, s.message_count, 0) "
             "FROM sessions s WHERE s.started_at >= ? AND s.started_at < ? AND s.source NOT IN (%s)" % ",".join("?" * len(MAIN_SOURCES)))
        try:
            cur = con.execute(q, [*hb_args, SILENT, lo, hi, *MAIN_SOURCES])
        except sqlite3.OperationalError:              # 老库没有 api_call_count
            cur = con.execute(q.replace("coalesce(s.api_call_count, s.message_count, 0)", "coalesce(s.message_count, 0)"),
                              [*hb_args, SILENT, lo, hi, *MAIN_SOURCES])
        for source, model, started, cost, tokens, cw, is_hb, silent, calls in cur:
            if not cost and not tokens:
                continue                                # no_agent 脚本 job：没调模型
            if source == "cron":
                src = "hermes_cron_heartbeat" if is_hb else "hermes_cron_other"
            else:
                src = "hermes_local"
            rows.append({"day": la_day(started), "source": src, "model": norm_model(model),
                         "task_id": f"cron:{HEARTBEAT_JOB_ID}" if src == "hermes_cron_heartbeat" else None,
                         "template": None, "cost_usd": round(_num(cost), 6), "tokens": int(tokens), "turns": int(calls or 0),
                         "denials": 0, "subtype": None, "exit_code": None, "billing": "plan",
                         "cache_create": int(cw), "idle": bool(is_hb and silent)})
        # 长会话：总成本 × 当天 assistant 消息数 / 全部 assistant 消息数（近似：不同回合上下文长度不同）
        mains = con.execute(f"SELECT s.id, s.model, coalesce(s.estimated_cost_usd,0), {tok} FROM sessions s WHERE s.source IN (%s)"
                            % ",".join("?" * len(MAIN_SOURCES)), MAIN_SOURCES).fetchall()
        for sid, model, cost, tokens in mains:
            total = con.execute("SELECT count(*) FROM messages WHERE session_id = ? AND role = 'assistant'", (sid,)).fetchone()[0]
            if not total or (not cost and not tokens):
                continue
            per_day = defaultdict(int)
            for (ts,) in con.execute("SELECT timestamp FROM messages WHERE session_id = ? AND role = 'assistant' AND timestamp >= ? AND timestamp < ?",
                                     (sid, lo, hi)):
                d = la_day(_num(ts))
                if d:
                    per_day[d] += 1
            for d, n in per_day.items():
                share = n / total
                rows.append({"day": d, "source": "hermes_main", "model": norm_model(model), "task_id": None, "template": None,
                             "cost_usd": round(_num(cost) * share, 6), "tokens": int(tokens * share), "turns": n, "denials": 0,
                             "subtype": None, "exit_code": None, "billing": "plan", "cache_create": 0, "idle": False})
    finally:
        con.close()
    return rows


def load_rows(since=None, until=None, dispatch=DISPATCH, db_path=STATE_DB, jobs_path=CRON_JOBS):
    rows = worker_rows(dispatch, since) + hermes_rows(db_path, since, jobs_path, until)
    return [r for r in rows if r["day"] and (not since or r["day"] >= since) and (not until or r["day"] <= until)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", help="只看这一天（YYYY-MM-DD，洛杉矶时区）")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="输出全部 rows（JSON）")
    a = ap.parse_args(argv)
    today = dt.datetime.now(TZ).date()
    since = a.day or (today - dt.timedelta(days=a.days - 1)).isoformat()
    rows = load_rows(since, a.day)
    if a.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 0
    agg = defaultdict(lambda: [0.0, 0, 0])
    for r in rows:
        a_ = agg[(r["day"], r["source"])]
        a_[0] += r["cost_usd"]; a_[1] += 1; a_[2] += r["tokens"]
    for (d, s), (c, n, t) in sorted(agg.items()):
        print(f"{d}  {s:<24} ${c:8.2f}  rows={n:<4} tokens={t / 1e6:.1f}M")
    return 0


if __name__ == "__main__":
    sys.exit(main())
