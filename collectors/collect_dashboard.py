#!/usr/bin/env python3
"""采集器 #2：Obsidian Dashboard 项目卡（只读 vault）。

契约见 vault 的 Dashboard/README.md：frontmatter tier/status/next/deadline/updated/progress，
正文 `## 现状` 首段、`## 里程碑` checkbox。读 Drive 同步目录可能卡住，所以真正的解析放在
子进程里，外层 30 秒硬超时；超时即本轮失败，由 run_all 记进 status.json。
"""
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys

from common import die, log, save_snapshot

VAULT_DASH = os.environ.get(
    "AI_HOME_DASHBOARD", os.path.expanduser("~/Sync/My Drive/2nd Brain/Hang/Dashboard"))
TIMEOUT = 30
DUSTY_DAYS = 30
STATE = {"active": "active", "waiting": "waiting", "you": "you", "ice": "ice", "done": "done"}


def slugify(name):
    """文件名 → slug：去 `proj:` 前缀与 emoji/标点，小写，空白→`-`（中文保留）。"""
    s = re.sub(r"^proj:\s*", "", name.strip(), flags=re.I).lower()
    return re.sub(r"[^\w]+", "-", s).strip("-_") or "card"


def card_id(fm_id, name):
    """卡 frontmatter 的 `id:` 优先（接受 `p:xxx` 或裸 slug），否则由文件名生成。"""
    if fm_id:
        return "p:" + slugify(re.sub(r"^p:", "", fm_id.strip(), flags=re.I))
    return "p:" + slugify(name)


def dedupe_ids(cards):
    """slug 冲突加序号（-2、-3…），按文件名排序决定先后，保证稳定。"""
    seen = {}
    for c in cards:
        base = c["id"]
        n = seen.get(base, 0) + 1
        seen[base] = n
        if n > 1:
            c["id"] = f"{base}-{n}"


def parse_card(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    fm, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            km = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
            if km:
                fm[km.group(1)] = km.group(2).strip().strip('"').strip("'")
    sections, cur = {}, None
    for line in body.splitlines():
        hm = re.match(r"^##\s+(.+?)\s*$", line)
        if hm:
            cur = hm.group(1)
            sections[cur] = []
        elif cur:
            sections[cur].append(line)
    summary = ""
    for para in "\n".join(sections.get("现状", [])).split("\n\n"):
        if para.strip():
            summary = re.sub(r"\s+", " ", para.strip())
            break
    done = total = 0
    for line in sections.get("里程碑", []):
        cm = re.match(r"^\s*[-*]\s+\[( |x|X)\]", line)
        if cm:
            total += 1
            done += cm.group(1).lower() == "x"
    progress = None
    if fm.get("progress", "").isdigit():
        progress = int(fm["progress"])
    elif total:
        progress = round(100 * done / total)
    dusty = False
    try:
        dusty = (dt.date.today() - dt.date.fromisoformat(fm.get("updated", "")[:10])).days > DUSTY_DAYS
    except ValueError:
        pass
    name = os.path.splitext(os.path.basename(path))[0]
    return {
        "id": card_id(fm.get("id"), name),
        "title": fm.get("title") or name,
        "icon": fm.get("icon", ""),
        "tier": fm.get("tier", ""),
        "status": fm.get("status", ""),
        "state": STATE.get(fm.get("status", ""), "active"),
        "next": fm.get("next", ""),
        "deadline": fm.get("deadline", ""),
        "deadline_note": fm.get("deadline_note", ""),
        "updated": fm.get("updated", ""),
        "progress": progress,
        "milestones": {"done": done, "total": total},
        "summary": summary[:400],
        "dusty": dusty,
        "ref": f"Dashboard/{name}.md",
        "obsidian": "obsidian://open?vault=Hang&file=" + subprocess_quote(f"Dashboard/{name}"),
    }


def subprocess_quote(s):
    from urllib.parse import quote
    return quote(s, safe="")


def worker():
    cards = []
    for path in sorted(glob.glob(os.path.join(VAULT_DASH, "*.md"))):
        if os.path.basename(path) == "README.md":
            continue
        cards.append(parse_card(path))
    dedupe_ids(cards)
    json.dump(cards, sys.stdout, ensure_ascii=False)


def main():
    if "--worker" in sys.argv:
        worker()
        return
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--worker"],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        die(f"dashboard FAIL: 读 vault 超过 {TIMEOUT}s，本轮跳过")
    if r.returncode != 0:
        die(f"dashboard FAIL: worker rc={r.returncode}: {r.stderr.strip()[:300]}")
    cards = json.loads(r.stdout)
    if not cards:
        die("dashboard FAIL: 没读到任何卡片（目录空或路径不对）")
    save_snapshot("dashboard", {"cards": cards})
    log(f"dashboard ok: {len(cards)} cards")


if __name__ == "__main__":
    main()
