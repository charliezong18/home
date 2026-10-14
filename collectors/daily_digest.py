#!/usr/bin/env python3
"""夜间「养卡」用的当日活动摘要:Hermes 会话 + Claude transcript + AI 主页编辑 → 一个 markdown。

用法: python3 daily_digest.py [--date YYYY-MM-DD] [--out PATH] [--max-chars 60000]
stdout 一行: digest <date> sessions=<h>+<c> edits=<n> chars=<n> path=<path>
无任何内容: digest <date> empty (exit 0,不建文件)。纯标准库,全部只读。
"""
import argparse, glob, json, os, re, sqlite3, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo("America/Los_Angeles")
HERMES_DB = os.path.expanduser("~/.hermes/state.db")
CLAUDE_ROOT = os.path.expanduser("~/.claude/projects")
EDITS = os.path.expanduser("~/Developer/review-home-data/edits.jsonl")
SKIP_PREFIX = ("[IMPORTANT:", "[ASYNC", "[OUT-OF-BAND")
DISPATCH = "你是 Claude 执行者"


def cut(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " …〔截断〕"


def fence(s):
    """多行内容缩进成引用块,避免内容里的 # 污染文件层级。"""
    return "\n".join("> " + ln for ln in s.splitlines()) or ">"


def day_bounds(target):
    d = datetime.strptime(target, "%Y-%m-%d").replace(tzinfo=LOCAL)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def hermes_blocks(target):
    if not os.path.exists(HERMES_DB):
        return []
    lo, hi = day_bounds(target)
    con = sqlite3.connect("file:%s?mode=ro" % HERMES_DB, uri=True)
    try:
        rows = con.execute(
            "SELECT session_id, role, content, timestamp FROM messages "
            "WHERE role IN ('user','assistant') AND timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp", (lo, hi)).fetchall()
    finally:
        con.close()
    groups = {}
    for sid, role, content, ts in rows:
        txt = (content or "").strip()
        if not txt or txt.startswith(SKIP_PREFIX):
            continue
        groups.setdefault(sid, []).append((role, txt, ts))
    blocks = []
    for sid, msgs in groups.items():
        t0 = datetime.fromtimestamp(msgs[0][2], LOCAL).strftime("%H:%M")
        t1 = datetime.fromtimestamp(msgs[-1][2], LOCAL).strftime("%H:%M")
        lines = ["### %s | %s–%s" % (sid[:8], t0, t1)]
        for role, txt, _ in msgs:
            if role == "user":
                lines.append("**我**:\n" + fence(cut(txt, 600)))
            else:
                lines.append("**Hermes**:\n" + fence(cut(txt, 400)))
        blocks.append((msgs[0][2], "\n\n".join(lines)))
    return blocks


def text_of(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def local_dt(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(LOCAL)
    except Exception:
        return None


def summarize_prompt(txt):
    """派工 prompt 只取「原始问题」与「任务背景」前 500 字。"""
    if not txt.startswith(DISPATCH):
        return cut(txt, 800)
    tid = re.search(r"任务ID[::]\s*(\S+)", txt)
    q = re.search(r"原始问题[^\n]*\n(.*?)(?:\n\s*\n任务背景|\n任务背景|$)", txt, re.S)
    b = re.search(r"任务背景[^\n]*\n(.*)", txt, re.S)
    out = ["(派工) 任务ID: %s" % (tid.group(1) if tid else "?")]
    out.append("原始问题: " + cut(q.group(1) if q else "", 800))
    out.append("任务背景: " + cut(b.group(1) if b else "", 500))
    return "\n".join(out)


def claude_blocks(target):
    blocks = []
    for path in glob.glob(os.path.join(CLAUDE_ROOT, "*", "*.jsonl")):
        first_user = last_asst = first_dt = last_dt = None
        try:
            fh = open(path, encoding="utf-8")
        except Exception:
            continue
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                ts = o.get("timestamp")
                dt = local_dt(ts) if ts else None
                if not dt or dt.date().isoformat() != target or o.get("isSidechain"):
                    continue
                t = o.get("type")
                txt = text_of((o.get("message") or {}).get("content"))
                if not txt:
                    continue
                if t == "user":
                    if txt.startswith("<") or txt.startswith("Caveat:"):
                        continue
                    if first_user is None:
                        first_user = txt
                elif t == "assistant":
                    last_asst = txt
                else:
                    continue
                first_dt = first_dt or dt
                last_dt = dt
        if first_user is None:
            continue
        proj = os.path.basename(os.path.dirname(path))
        sid = os.path.basename(path)[:-6]
        lines = ["### %s | %s | %s–%s" % (sid[:8], proj, first_dt.strftime("%H:%M"),
                                          last_dt.strftime("%H:%M")),
                 "**起手**:\n" + fence(summarize_prompt(first_user))]
        if last_asst:
            lines.append("**结论**:\n" + fence(cut(last_asst, 1200)))
        blocks.append((first_dt.timestamp(), "\n\n".join(lines)))
    return blocks


def edit_blocks(target):
    out = []
    if not os.path.exists(EDITS):
        return out
    with open(EDITS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            stamp = o.get("created") or o.get("ts")
            if not isinstance(stamp, str):
                continue
            dt = local_dt(stamp)
            day = dt.date().isoformat() if dt else stamp[:10]
            if day == target:
                out.append((dt.timestamp() if dt else 0.0, line))
    return out


def squeeze(blocks, budget):
    """按块均匀截断:短块原样,超出的块共享剩余预算。"""
    n = len(blocks)
    if not n:
        return blocks
    sizes = sorted(len(b) for b in blocks)
    left, cap = budget, None
    for i, s in enumerate(sizes):
        share = left // (n - i)
        if s > share:
            cap = share
            break
        left -= s
    if cap is None:
        return blocks
    cap = max(cap, 200)
    return [b if len(b) <= cap else b[:cap] + " …〔截断〕" for b in blocks]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-chars", type=int, default=60000)
    a = ap.parse_args()
    target = a.date or datetime.now(LOCAL).date().isoformat()
    out = a.out or os.path.expanduser("~/Library/Caches/ai-home/digest-%s.md" % target)

    h = [b for _, b in sorted(hermes_blocks(target))]
    c = [b for _, b in sorted(claude_blocks(target))]
    e = [b for _, b in sorted(edit_blocks(target))]
    if not (h or c or e):
        print("digest %s empty" % target)
        return

    total = sum(len(x) for x in h + c + e)
    note = ""
    overhead = 800 + 3 * len(h + c + e)  # 标题/分隔符/注记
    if total + overhead > a.max_chars:
        allb = squeeze(h + c + e, max(a.max_chars - overhead, 1000))
        h, c, e = allb[:len(h)], allb[len(h):len(h) + len(c)], allb[len(h) + len(c):]
        note = "> 注:原文 %d 字符超过 --max-chars=%d,已按 session 均匀截断。\n\n" % (total, a.max_chars)

    parts = ["# Digest %s\n" % target, note,
             "## Hermes 会话\n", "\n\n".join(h) if h else "(无)", "\n",
             "## Claude 会话\n", "\n\n".join(c) if c else "(无)", "\n",
             "## 主页编辑\n", ("```jsonl\n" + "\n".join(e) + "\n```") if e else "(无)", "\n"]
    text = "\n".join(p for p in parts if p != "")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("digest %s sessions=%d+%d edits=%d chars=%d path=%s" % (target, len(h), len(c), len(e), len(text), out))


if __name__ == "__main__":
    main()
