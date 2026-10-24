#!/usr/bin/env python3
"""门下画可 → 执行接管台账。只检测、记账、回写标签；不调 claude、不派工（派工归 Hermes）。

画可（merge）≠ 完成，也 ≠ 概括授权：merge 只生成一张「待接手」单，带上他在折里的判（原文）
与归属的 Hermes 会话；执行范围由 Hermes 读全文后在 claim 时显式写进 --scope。

数据源：GitHub REST（gh api，只读）`pulls?state=closed&sort=updated`，按游标增量；
游标只在整轮成功后前移，且每轮回看 OVERLAP，进程停掉/重启/某轮失败后下一轮自动补漏。

幂等键：repo#PR@merge_sha。同一 merge 重复出现（重复事件、重叠窗口、重启重扫）＝ no-op。
kind:读物 / kind:参考 → 记 reference（仅留档，不生成执行单）。

状态：pending 待接手 → executing 执行中 → verifying 验收 → done 完成；blocked 阻塞 旁路。
  done 只能从 verifying 来且必须 --evidence；done 是终态。
门下回写（--writeback，仅改 label，不发会话区评论——会变成假待办）：
  pending/executing/verifying: −wait:你拍 −wait:你读 +wait:agent；executing/verifying 另加 agent:claimed
  blocked: −wait:agent −agent:claimed +agent:blocked +wait:你拍
  done: 去掉 wait:* 与 agent:*

用法：
  approvals.py scan [--since ISO] [--writeback]   增量检测 merge（launchd 每 5 分钟跑，不带 --writeback）
  approvals.py list [--state S] [--stale-hours H]  JSON；Hermes 心跳用 `list --state pending`
  approvals.py show --key K | --pr N
  approvals.py claim --key K --owner-session S --task-id T --scope 文本 [--event-id E] [--writeback]
  approvals.py transition --key K --to STATE [--evidence 文本] [--reason 文本] [--event-id E] [--writeback]
  approvals.py set-owner --key K --owner-session S
  approvals.py sync --key K                         按台账状态回写门下 label（读回核对）
状态目录：$MENXIA_APPROVALS_STATE，默认 ~/.local/state/menxia-approvals/（approvals.json + events.log）。
"""
import argparse
import datetime as _dt
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager

REPO = os.environ.get("MENXIA_REPO", "charliezong18/review")
STATE_DIR = os.path.expanduser(os.environ.get("MENXIA_APPROVALS_STATE", "~/.local/state/menxia-approvals"))
HERMES_DB = os.path.expanduser(os.environ.get("MENXIA_HERMES_DB", "~/.hermes/state.db"))
OVERLAP = _dt.timedelta(hours=6)
BOOTSTRAP = _dt.timedelta(hours=24)
REFERENCE_KINDS = {"读物", "参考"}
STATES = {"pending": "待接手", "executing": "执行中", "verifying": "验收", "done": "完成", "blocked": "阻塞"}
ALLOWED = {
    "pending": {"executing", "blocked"},
    "executing": {"verifying", "blocked"},
    "verifying": {"done", "executing", "blocked"},
    "blocked": {"pending", "executing"},
    "done": set(),
}
AUTO_MARKERS = ("<!-- menxia-readstate", "**回话**")

os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "/usr/bin:/bin")


def now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(s):
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def log(msg):
    print(f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------- GitHub（只读拉取，可在测试里替换） ----------

def gh_json(path):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path} rc={r.returncode}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout or "null")


def fetch_merged_since(since, gh=gh_json):
    """closed PR 按 updated 倒序翻页，updated < since 即停（merged ⇒ updated ≥ merged_at，不会漏）。"""
    out, page = [], 1
    while True:
        batch = gh(f"repos/{REPO}/pulls?state=closed&sort=updated&direction=desc&per_page=50&page={page}")
        if not batch:
            break
        for p in batch:
            if p.get("merged_at") and parse(p["merged_at"]) >= since:
                out.append(p)
        if parse(batch[-1]["updated_at"]) < since or len(batch) < 50:
            break
        page += 1
    return out


def fetch_decisions(n, gh=gh_json):
    """他在折里的判（原文）：会话区评论 + 行内评论，去掉续读标记与 agent 的 **回话**。"""
    items = []
    for kind, path in (("conversation", f"repos/{REPO}/issues/{n}/comments?per_page=100"),
                       ("inline", f"repos/{REPO}/pulls/{n}/comments?per_page=100")):
        for c in gh(path) or []:
            body = (c.get("body") or "").strip()
            if not body or body.startswith(AUTO_MARKERS):
                continue
            items.append({"id": c["id"], "where": kind, "at": c["created_at"],
                          "path": c.get("path"), "body": body[:2000]})
    return sorted(items, key=lambda x: x["at"])


# ---------- 归属 Hermes 会话 ----------

def resolve_owner(pr, db=None):
    """merge 前在 Hermes 消息里提到该折分支名 / pull/N 次数最多的非 cron 会话；并列或为零 → None。"""
    db = db or HERMES_DB
    if not os.path.exists(db):
        return None, "hermes state.db 不存在"
    terms = [t for t in (pr.get("head", {}).get("ref"), f"review/pull/{pr['number']}") if t]
    cutoff = parse(pr["merged_at"]).timestamp()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        counts = {}
        for t in terms:
            rows = con.execute(
                "SELECT m.session_id, count(*) FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE s.source != 'cron' AND m.timestamp <= ? AND (m.content LIKE ? OR m.tool_calls LIKE ?) "
                "GROUP BY m.session_id", (cutoff, f"%{t}%", f"%{t}%")).fetchall()
            for sid, c in rows:
                counts[sid] = counts.get(sid, 0) + c
    finally:
        con.close()
    if not counts:
        return None, f"merge 前无会话提到 {terms}"
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, f"并列：{ranked[:3]}"
    return ranked[0][0], f"命中 {terms}：{ranked[:3]}"


# ---------- 台账 ----------

def paths():
    return os.path.join(STATE_DIR, "approvals.json"), os.path.join(STATE_DIR, "events.log")


@contextmanager
def ledger():
    os.makedirs(STATE_DIR, exist_ok=True)
    path, _ = paths()
    with open(path + ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            data = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            data.setdefault("cursor", None)
            data.setdefault("jobs", {})
            data.setdefault("references", {})
            yield data
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def event(msg):
    _, ev = paths()
    with open(ev, "a", encoding="utf-8") as f:
        f.write(f"{iso(now())} {msg}\n")


def key_of(pr):
    return f"{REPO}#{pr['number']}@{pr['merge_commit_sha']}"


def labels_of(pr):
    return [l["name"] for l in pr.get("labels", [])]


def kind_of(labels):
    return next((l[5:] for l in labels if l.startswith("kind:")), None)


def section(body, title):
    """PR body 里 `## title` 一节的正文（呈折模板固定有「待你拍板」「目的地」）。"""
    lines, keep = [], False
    for ln in (body or "").splitlines():
        if ln.startswith("## "):
            keep = ln[3:].strip() == title
            continue
        if keep:
            lines.append(ln)
    return "\n".join(lines).strip() or None


def ingest(data, pr, gh=gh_json, db=None):
    """返回 'new-job' / 'new-reference' / 'dup'。"""
    k = key_of(pr)
    if k in data["jobs"] or k in data["references"]:
        return "dup"
    labels = labels_of(pr)
    base = {"key": k, "repo": REPO, "pr": pr["number"], "title": pr["title"], "merge_sha": pr["merge_commit_sha"],
            "merged_at": pr["merged_at"], "merged_by": (pr.get("merged_by") or {}).get("login"),
            "labels_at_merge": labels, "detected_at": iso(now())}
    if kind_of(labels) in REFERENCE_KINDS:
        data["references"][k] = {**base, "note": "读物/参考：画可＝读毕，不生成执行单"}
        event(f"reference {k}")
        return "new-reference"
    owner, why = resolve_owner(pr, db)
    data["jobs"][k] = {
        **base, "state": "pending", "state_zh": STATES["pending"],
        "merge_is_authorization": False,
        "authorized_scope": None,
        "pending_decisions": section(pr.get("body"), "待你拍板"),
        "destination": section(pr.get("body"), "目的地"),
        "decisions": fetch_decisions(pr["number"], gh),
        "owner_session": owner, "owner_reason": why,
        "task_id": None, "evidence": None, "block_reason": None,
        "history": [{"at": iso(now()), "to": "pending", "by": "scan"}], "event_ids": [],
        "writeback": None,
    }
    event(f"job {k} owner={owner}")
    return "new-job"


def scan(since=None, gh=gh_json, db=None, writeback=False):
    t0 = now()
    with ledger() as data:
        cur = parse(data["cursor"]) if data["cursor"] else None
        start = since or ((cur - OVERLAP) if cur else t0 - BOOTSTRAP)
        prs = fetch_merged_since(start, gh)
        res = {"new-job": [], "new-reference": [], "dup": []}
        for pr in sorted(prs, key=lambda p: p["merged_at"]):
            res[ingest(data, pr, gh, db)].append(pr["number"])
        if not since:  # --since 是手动补扫，不动游标
            data["cursor"] = iso(t0)
        new_keys = [k for k, j in data["jobs"].items() if j["pr"] in res["new-job"]]
    if writeback:
        for k in new_keys:
            sync(k, gh)
    log(f"scan since={iso(start)} merged={len(prs)} new_jobs={res['new-job']} "
        f"references={res['new-reference']} dup={res['dup']}")
    return res


def find(data, key=None, pr=None):
    if key:
        return data["jobs"].get(key)
    hits = [j for j in data["jobs"].values() if j["pr"] == pr]
    return hits[-1] if hits else None


def transition(key, to, evidence=None, reason=None, event_id=None, by="hermes", **extra):
    with ledger() as data:
        j = data["jobs"].get(key)
        if not j:
            raise SystemExit(f"no job {key}")
        if event_id and event_id in j["event_ids"]:
            return j, "dup-event"
        if to not in ALLOWED[j["state"]]:
            raise SystemExit(f"illegal {j['state']} -> {to}")
        if to == "done" and not evidence:
            raise SystemExit("done 需要 --evidence")
        if to == "blocked" and not reason:
            raise SystemExit("blocked 需要 --reason")
        if to == "executing" and not (extra.get("authorized_scope") or j["authorized_scope"]):
            raise SystemExit("executing 需要 Hermes 读全文后给 --scope（merge 不是概括授权）")
        for k, v in extra.items():
            if v is not None:
                j[k] = v
        j["state"], j["state_zh"] = to, STATES[to]
        if evidence:
            j["evidence"] = evidence
        j["block_reason"] = reason if to == "blocked" else None
        j["history"].append({"at": iso(now()), "to": to, "by": by, "event_id": event_id,
                             "evidence": evidence, "reason": reason})
        if event_id:
            j["event_ids"].append(event_id)
        event(f"{key} -> {to}")
        return dict(j), "ok"


# ---------- 门下回写（label） ----------

def want_labels(state):
    if state == "done":
        return set(), {"wait:agent", "wait:你拍", "wait:你读", "wait:闲", "agent:claimed", "agent:blocked"}
    if state == "blocked":
        return {"agent:blocked", "wait:你拍"}, {"wait:agent", "agent:claimed", "wait:你读"}
    add = {"wait:agent"} | ({"agent:claimed"} if state in ("executing", "verifying") else set())
    return add, {"wait:你拍", "wait:你读", "wait:闲", "agent:blocked"} | ({"agent:claimed"} - add)


def sync(key, gh=gh_json, edit=None):
    with ledger() as data:
        j = dict(data["jobs"][key])
    add, rm = want_labels(j["state"])
    cur = set(labels_of(gh(f"repos/{REPO}/pulls/{j['pr']}")))
    to_add, to_rm = sorted(add - cur), sorted(rm & cur)
    if to_add or to_rm:
        cmd = ["gh", "pr", "edit", str(j["pr"]), "-R", REPO]
        for l in to_add:
            cmd += ["--add-label", l]
        for l in to_rm:
            cmd += ["--remove-label", l]
        (edit or (lambda c: subprocess.run(c, check=True, capture_output=True, text=True, timeout=60)))(cmd)
    after = sorted(labels_of(gh(f"repos/{REPO}/pulls/{j['pr']}")))
    ok = add <= set(after) and not (rm & set(after))
    with ledger() as data:
        data["jobs"][key]["writeback"] = {"at": iso(now()), "state": j["state"], "labels": after, "verified": ok}
    event(f"sync {key} state={j['state']} labels={after} verified={ok}")
    return {"key": key, "state": j["state"], "added": to_add, "removed": to_rm, "labels_readback": after,
            "verified": ok}


# ---------- CLI ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--since")
    s.add_argument("--writeback", action="store_true")
    s = sub.add_parser("list")
    s.add_argument("--state", choices=list(STATES))
    s.add_argument("--stale-hours", type=float)
    s.add_argument("--references", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("--key")
    s.add_argument("--pr", type=int)
    s = sub.add_parser("claim")
    for a in ("--key", "--owner-session", "--task-id", "--scope"):
        s.add_argument(a, required=True)
    s.add_argument("--event-id")
    s.add_argument("--writeback", action="store_true")
    s = sub.add_parser("transition")
    s.add_argument("--key", required=True)
    s.add_argument("--to", required=True, choices=list(STATES))
    for a in ("--evidence", "--reason", "--event-id", "--task-id"):
        s.add_argument(a)
    s.add_argument("--writeback", action="store_true")
    s = sub.add_parser("set-owner")
    s.add_argument("--key", required=True)
    s.add_argument("--owner-session", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--key", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "scan":
        scan(parse(a.since) if a.since else None, writeback=a.writeback)
    elif a.cmd == "list":
        with ledger() as data:
            if a.references:
                print(json.dumps(list(data["references"].values()), ensure_ascii=False, indent=2))
                return
            jobs = [j for j in data["jobs"].values() if not a.state or j["state"] == a.state]
            if a.stale_hours is not None:
                lim = now() - _dt.timedelta(hours=a.stale_hours)
                jobs = [j for j in jobs if j["state"] not in ("done",) and parse(j["history"][-1]["at"]) < lim]
            print(json.dumps(jobs, ensure_ascii=False, indent=2))
    elif a.cmd == "show":
        with ledger() as data:
            print(json.dumps(find(data, a.key, a.pr), ensure_ascii=False, indent=2))
    elif a.cmd == "claim":
        j, st = transition(a.key, "executing", event_id=a.event_id, owner_session=a.owner_session,
                           task_id=a.task_id, authorized_scope=a.scope)
        print(json.dumps({"result": st, "state": j["state"], "scope": j["authorized_scope"]}, ensure_ascii=False))
        if a.writeback:
            print(json.dumps(sync(a.key), ensure_ascii=False))
    elif a.cmd == "transition":
        j, st = transition(a.key, a.to, a.evidence, a.reason, a.event_id, task_id=a.task_id)
        print(json.dumps({"result": st, "state": j["state"]}, ensure_ascii=False))
        if a.writeback:
            print(json.dumps(sync(a.key), ensure_ascii=False))
    elif a.cmd == "set-owner":
        with ledger() as data:
            data["jobs"][a.key]["owner_session"] = a.owner_session
            data["jobs"][a.key]["owner_reason"] = "Hermes 手动指定"
        event(f"{a.key} owner={a.owner_session}")
    elif a.cmd == "sync":
        print(json.dumps(sync(a.key), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:  # gh 失败：不动游标，下一轮自然补扫
        print(f"approvals FAIL: {e}", file=sys.stderr)
        sys.exit(1)
