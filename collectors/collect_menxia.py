#!/usr/bin/env python3
"""采集器 #1：门下折务 + 弘文馆。gh api 读 review 仓 open PR 与 labels，写本地快照。"""
import datetime as dt
import json
import re
import sys

from common import REPO, die, log, run, save_snapshot

OLDEST_N = 20
RECENT_N = 50            # 最近更新的已关 PR 取一页，够覆盖一两天的画可
RECENT_DAYS = 2
DUE_PREFIX = "due:"
DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DESK = "https://menxia-reader.github.io/menxia/?pr={n}"
WAITS = ["你拍", "你读", "agent", "闲"]


def label_values(labels, prefix):
    return [l["name"][len(prefix):] for l in labels if l["name"].startswith(prefix)]


def folder_item(p):
    labels = p.get("labels", [])
    waits = label_values(labels, "wait:")
    dues = []
    for v in label_values(labels, DUE_PREFIX):
        if DUE_RE.match(v):
            dues.append(v)
        else:
            log(f"menxia: #{p['number']} due label 格式不对: {v}")
    return {
        "n": p["number"],
        "title": p["title"],
        "url": DESK.format(n=p["number"]),
        "created": p["created_at"],
        "updated": p["updated_at"],
        "draft": bool(p.get("draft")),
        "wait": waits[0] if waits else None,
        "kind": (label_values(labels, "kind:") or [None])[0],
        "proj": label_values(labels, "proj:"),
        "qian": label_values(labels, "签:"),          # 早晚报据此脱敏（签:身份 / 签:健康 / 签:钱 …）
        "deadline": min(dues) if dues else "",
        "deadline_note": "",
    }


def recent_merged(prs, now=None):
    """已关 PR（按 updated 倒序）→ 近 RECENT_DAYS 天合并（画可）的折 [{n, title, merged_at, kind, proj, qian}]。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for p in prs:
        if p.get("merged_at") and p["merged_at"] >= cutoff:
            labels = p.get("labels", [])
            out.append({"n": p["number"], "title": p["title"], "merged_at": p["merged_at"],
                        "kind": (label_values(labels, "kind:") or [None])[0],
                        "proj": label_values(labels, "proj:"), "qian": label_values(labels, "签:")})
    return sorted(out, key=lambda x: x["merged_at"], reverse=True)


def main():
    r = run(["gh", "api", "--paginate", f"repos/{REPO}/pulls?state=open&per_page=100"], timeout=60)
    # --paginate 会把多页 JSON 数组首尾相接
    raw = r.stdout.strip().replace("][", ",")
    prs = json.loads(raw or "[]")
    items = [folder_item(p) for p in prs]
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
    try:
        data["recent_closed"] = recent_merged(json.loads(run(
            ["gh", "api", f"repos/{REPO}/pulls?state=closed&sort=updated&direction=desc&per_page={RECENT_N}"],
            timeout=60).stdout or "[]"))
    except Exception as e:  # noqa: BLE001 — 晚报的「今天画可」拿不到不拖垮主采集
        log(f"menxia: 最近画可的折读取失败：{str(e)[:200]}")
    save_snapshot("menxia", data)
    log(f"menxia ok: total={len(items)} counts={json.dumps(counts, ensure_ascii=False)} library={len(library)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — 任何失败都交给 run_all 记 status
        die(f"menxia FAIL: {e}")
