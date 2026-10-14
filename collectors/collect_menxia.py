#!/usr/bin/env python3
"""采集器 #1：门下折务 + 弘文馆。gh api 读 review 仓 open PR 与 labels，写本地快照。"""
import json
import sys

from common import REPO, die, log, run, save_snapshot

OLDEST_N = 8
DESK = "https://charliezong18.github.io/menxia/?pr={n}"
WAITS = ["你拍", "你读", "agent", "闲"]


def label_values(labels, prefix):
    return [l["name"][len(prefix):] for l in labels if l["name"].startswith(prefix)]


def main():
    r = run(["gh", "api", "--paginate", f"repos/{REPO}/pulls?state=open&per_page=100"], timeout=60)
    # --paginate 会把多页 JSON 数组首尾相接
    raw = r.stdout.strip().replace("][", ",")
    prs = json.loads(raw or "[]")
    items = []
    for p in prs:
        labels = p.get("labels", [])
        waits = label_values(labels, "wait:")
        items.append({
            "n": p["number"],
            "title": p["title"],
            "url": DESK.format(n=p["number"]),
            "created": p["created_at"],
            "updated": p["updated_at"],
            "draft": bool(p.get("draft")),
            "wait": waits[0] if waits else None,
            "kind": (label_values(labels, "kind:") or [None])[0],
            "proj": label_values(labels, "proj:"),
        })
    counts = {w: 0 for w in WAITS}
    counts["未标注"] = 0
    groups = {w: [] for w in WAITS + ["未标注"]}
    for it in items:
        key = it["wait"] if it["wait"] in WAITS else "未标注"
        counts[key] += 1
        groups[key].append(it)
    for key in groups:
        groups[key] = sorted(groups[key], key=lambda x: x["created"])[:OLDEST_N]
    library = sorted([it for it in items if it["kind"] == "读物"], key=lambda x: x["created"], reverse=True)
    data = {"total": len(items), "counts": counts, "oldest": groups, "library": library}
    save_snapshot("menxia", data)
    log(f"menxia ok: total={len(items)} counts={json.dumps(counts, ensure_ascii=False)} library={len(library)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — 任何失败都交给 run_all 记 status
        die(f"menxia FAIL: {e}")
