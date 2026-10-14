#!/usr/bin/env python3
"""验收用：用 `gh pr list --json labels`（与采集器不同的路径）手算 wait 计数，
再经 contents API 读回 home-data 上的 home.json，对照两边。"""
import base64
import collections
import json
import subprocess
import sys

REPO = "charliezong18/review"
prs = json.loads(subprocess.run(
    ["gh", "pr", "list", "-R", REPO, "--state", "open", "--limit", "500", "--json", "number,labels"],
    capture_output=True, text=True, check=True).stdout)
c = collections.Counter()
for p in prs:
    w = [l["name"][5:] for l in p["labels"] if l["name"].startswith("wait:")]
    c[w[0] if w else "未标注"] += 1
lib = sum(1 for p in prs if any(l["name"] == "kind:读物" for l in p["labels"]))
manual = {"total": len(prs), **dict(c), "读物": lib}

r = json.loads(subprocess.run(
    ["gh", "api", f"repos/{REPO}/contents/home.json?ref=home-data"],
    capture_output=True, text=True, check=True).stdout)
home = json.loads(base64.b64decode(r["content"]))
f = home["folders"]
remote = {"total": f["total"], **{k: v for k, v in f["counts"].items() if v}, "读物": len(home["library"])}
print("gh pr list 手算:", json.dumps(manual, ensure_ascii=False, sort_keys=True))
print("home.json 读回 :", json.dumps(remote, ensure_ascii=False, sort_keys=True))
print("home.json generated:", home["generated"], "sha:", r["sha"][:10],
      "projects:", len(home["projects"]), "readings:", len(home["readings"]))
print("sources:", json.dumps(home["sources"], ensure_ascii=False))
ok = manual == remote
print("MATCH" if ok else "MISMATCH")
sys.exit(0 if ok else 1)
