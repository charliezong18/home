#!/usr/bin/env python3
"""Backlog 采集器：读 vault「📋 Backlog.md」的「⏰ 有窗口的」表 → 快照 backlog。

表列：# | 事项 | 领域 | 谁干 | 窗口 / 备注。~~划线~~ 行（已结案）跳过；
第 5 列里抓第一个日期（YYYY-MM-DD / MM-DD / M月D日），抓不到的条目仍输出，date 为空。
读 vault 走子进程 + 超时（同 collect_dashboard）。
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote

from common import BACKLOG_REL, VAULT, die, log, save_snapshot

TIMEOUT = 30
SECTION = "有窗口的"
GRACE_DAYS = 14   # 无年份日期：过期不超过 14 天仍算今年，更早的算明年
DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(?<![\d/-])(\d{1,2})-(\d{1,2})(?![\d-])|(\d{1,2})月(\d{1,2})日")


def find_date(text, today):
    """第一个合法日期 → 'YYYY-MM-DD'，没有返回 ''。无年份的取最近的（过期 ≤14 天算今年，否则明年）。"""
    for m in DATE.finditer(text):
        try:
            if m.group(1):
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            mo, d = (int(m.group(4)), int(m.group(5))) if m.group(4) else (int(m.group(6)), int(m.group(7)))
            cand = dt.date(today.year, mo, d)
            if (today - cand).days > GRACE_DAYS:
                cand = dt.date(today.year + 1, mo, d)
            return cand.isoformat()
        except ValueError:
            continue
    return ""


def clean(s):
    s = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", s)    # [[a|b]] → b
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)           # [t](url) → t
    return re.sub(r"[*`]|~~", "", s).strip()


def parse_backlog(text, today):
    """文本 → [{title, date, note}]；只看含「有窗口的」的标题到下一个 ## 之间。"""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("#"):
            inside = line.startswith("## ") and SECTION in line
            continue
        if not inside or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or set(cells[0]) <= set("-: ") or cells[0] == "#":
            continue
        if "~~" in cells[0] or cells[1].startswith("~~"):
            continue
        note = cells[4]
        out.append({"title": clean(cells[1]), "date": find_date(note, today), "note": clean(note)[:80]})
    return out


def worker():
    with open(os.path.join(VAULT, BACKLOG_REL), encoding="utf-8") as f:
        items = parse_backlog(f.read(), dt.date.today())
    json.dump(items, sys.stdout, ensure_ascii=False)


def main():
    if "--worker" in sys.argv:
        worker()
        return
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--worker"],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        die(f"backlog FAIL: 读 vault 超过 {TIMEOUT}s，本轮跳过")
    if r.returncode != 0:
        die(f"backlog FAIL: worker rc={r.returncode}: {r.stderr.strip()[:300]}")
    items = json.loads(r.stdout)
    url = "obsidian://open?vault=Hang&file=" + quote(os.path.splitext(BACKLOG_REL)[0], safe="")
    save_snapshot("backlog", {"items": items, "url": url})
    log(f"backlog ok: n={len(items)} undated={sum(1 for i in items if not i['date'])}")


if __name__ == "__main__":
    main()
