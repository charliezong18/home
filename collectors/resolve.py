#!/usr/bin/env python3
"""ID 解析（给 Hermes 用）：把页面上复制来的 ID 还原成它指的那一条。

    resolve.py '#179' 'p:门下-menxia' 'r:20261013-b559'
    resolve.py --data /path/to/home.json '#179'

读本地 worktree 的 home.json（不走网络），每个 ID 打印一行 JSON；有找不到的 → 退出码 1。
ID 约定见 README「ID 约定」。折只在 home.json 收录范围内（每类最旧 8 折 + 弘文馆）。
"""
import json
import os
import sys

DATA = os.environ.get("AI_HOME_DATA", os.path.expanduser("~/Developer/review-home-data"))
VAULT = os.environ.get("AI_HOME_VAULT", os.path.expanduser("~/Sync/My Drive/2nd Brain/Hang"))


def folder_index(home):
    idx = {}
    folders = home.get("folders", {}).get("oldest", {})
    for group in list(folders.values()) + [home.get("library", [])]:
        for f in group:
            idx.setdefault(f["n"], f)
    return idx


def resolve(home, rid):
    k = rid.strip()
    if k.startswith("#") or k.isdigit():
        n = k.lstrip("#")
        f = folder_index(home).get(int(n)) if n.isdigit() else None
        if f:
            return {"id": f"#{f['n']}", "kind": "folder", "title": f["title"], "url": f["url"],
                    "meta": {"proj": f.get("proj"), "kind": f.get("kind"), "wait": f.get("wait")}}
    elif k.startswith("p:"):
        for p in home.get("projects", []):
            if p.get("id") == k:
                return {"id": k, "kind": "project", "title": p["title"],
                        "url": os.path.join(VAULT, p.get("ref", "")),
                        "meta": {x: p.get(x) for x in ("tier", "status", "next", "deadline")}}
    elif k.startswith("r:"):
        for r in home.get("readings", []):
            if r.get("rid") == k or "r:" + r.get("id", "") == k:
                return {"id": r["rid"], "kind": "reading", "title": r.get("title") or r["url"],
                        "url": r["url"], "meta": {"added": r.get("added"), "status": r.get("status")}}
    return None


def main(argv):
    data = os.path.join(DATA, "home.json")
    if argv[:1] == ["--data"]:
        data, argv = argv[1], argv[2:]
    if not argv:
        print("usage: resolve.py [--data home.json] <id> [<id>...]", file=sys.stderr)
        return 2
    with open(data, encoding="utf-8") as f:
        home = json.load(f)
    rc = 0
    for rid in argv:
        out = resolve(home, rid)
        if out is None:
            out, rc = {"id": rid, "error": "not found"}, 1
        print(json.dumps(out, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
