#!/usr/bin/env python3
"""验收用：看 home.json 里某张卡的编辑流标记。用法：check_edits.py [p:slug]"""
import json
import os
import sys

from common import DATA_DIR

target = sys.argv[1] if len(sys.argv) > 1 else "p:门下-menxia"
with open(os.path.join(DATA_DIR, "home.json"), encoding="utf-8") as f:
    d = json.load(f)
ps = d.get("projects", [])
p = next((x for x in ps if x.get("id") == target), None)
if not p:
    sys.exit(f"home.json 里没有 {target}")
print(json.dumps({k: p.get(k) for k in ("id", "next", "updated", "pending_edits", "hermes_edits", "milestones")}, ensure_ascii=False))
print("all cards carry pending_edits:", all("pending_edits" in x for x in ps),
      "| flagged:", [x["id"] for x in ps if x.get("pending_edits") or x.get("hermes_edits")])
