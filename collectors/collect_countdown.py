#!/usr/bin/env python3
"""倒数日采集器：读 vault 的 倒数日.md → 快照 countdown。

行格式：`- YYYY-MM-DD | 标签`（一次性）、`- YYYY-MM-DD 🔁 | 标签` / `- MM-DD 🔁 | 标签`（每年重复）。
读 vault 走子进程 + 超时（Drive/File Provider 卡死防护，同 collect_dashboard）。
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys

from common import COUNTDOWN_REL, VAULT, die, log, save_snapshot

TIMEOUT = 30
LINE = re.compile(r"^\s*[-*]\s*(?:(\d{4})-)?(\d{2})-(\d{2})\s*(🔁)?\s*\|\s*(.+?)\s*$")


def parse_countdown(text):
    """文本 → [{label, year(int|None), month, day, recur}]；不合格式的行跳过。"""
    out = []
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        year, mo, d = (int(m.group(1)) if m.group(1) else None), int(m.group(2)), int(m.group(3))
        try:
            dt.date(year or 2000, mo, d)   # 2000 是闰年，2-29 也合法
        except ValueError:
            continue
        out.append({"label": m.group(5), "year": year, "month": mo, "day": d, "recur": bool(m.group(4))})
    return out


def _on(year, month, day):
    try:
        return dt.date(year, month, day)
    except ValueError:   # 2-29 在平年落到 2-28
        return dt.date(year, month, 28)


def next_date(item, today):
    """年度条目 → 今天及以后的下一次发生日；一次性条目 → 原日期（无年份的一次性条目返回 None）。"""
    if not item.get("recur"):
        return dt.date(item["year"], item["month"], item["day"]) if item.get("year") else None
    d = _on(today.year, item["month"], item["day"])
    return d if d >= today else _on(today.year + 1, item["month"], item["day"])


def worker():
    with open(os.path.join(VAULT, COUNTDOWN_REL), encoding="utf-8") as f:
        json.dump(parse_countdown(f.read()), sys.stdout, ensure_ascii=False)


def main():
    if "--worker" in sys.argv:
        worker()
        return
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--worker"],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        die(f"countdown FAIL: 读 vault 超过 {TIMEOUT}s，本轮跳过")
    if r.returncode != 0:
        die(f"countdown FAIL: worker rc={r.returncode}: {r.stderr.strip()[:300]}")
    items = json.loads(r.stdout)
    if not items:
        die("countdown FAIL: 没解析出任何倒数日（文件空或格式变了）")
    save_snapshot("countdown", {"items": items})
    log(f"countdown ok: n={len(items)}")


if __name__ == "__main__":
    main()
