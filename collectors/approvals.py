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
  回写前先 gh api 只读核对要加的 label 在仓里存在，缺的 gh label create 补；写失败退出码 2，台账不回滚。

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
import re
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


def gh_pages(path, gh=gh_json, per_page=100):
    """翻完全部页（评论/label 超过一页时不截断）。"""
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while True:
        batch = gh(f"{path}{sep}per_page={per_page}&page={page}") or []
        out += batch
        if len(batch) < per_page:
            return out
        page += 1


def fetch_decisions(n, gh=gh_json):
    """他在折里的判（原文）：会话区评论 + 行内评论，去掉续读标记与 agent 的 **回话**。"""
    items = []
    for kind, path in (("conversation", f"repos/{REPO}/issues/{n}/comments"),
                       ("inline", f"repos/{REPO}/pulls/{n}/comments")):
        for c in gh_pages(path, gh):
            body = (c.get("body") or "").strip()
            if not body or body.startswith(AUTO_MARKERS):
                continue
            items.append({"id": c["id"], "where": kind, "at": c["created_at"],
                          "path": c.get("path"), "body": body[:2000]})
    return sorted(items, key=lambda x: x["at"])


# ---------- 归属 Hermes 会话 ----------

def term_re(term):
    """完整 token 匹配：前后不能紧贴字母数字/_/-，所以 pull/190 不中 pull/1901，branch-190 不中 branch-1900 / x-branch-190。"""
    return re.compile(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])")


def resolve_owner(pr, db=None):
    """merge 前在 Hermes 消息里提到该折分支名 / pull/N 次数最多的非 cron 会话；并列或为零 → None。
    SQL LIKE 只做粗筛，计数以 Python 正则的完整 token 命中为准（按消息条数计）。"""
    db = db or HERMES_DB
    if not os.path.exists(db):
        return None, "hermes state.db 不存在"
    terms = [t for t in (pr.get("head", {}).get("ref"), f"review/pull/{pr['number']}") if t]
    cutoff = parse(pr["merged_at"]).timestamp()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        counts = {}
        for t in terms:
            rx = term_re(t)
            rows = con.execute(
                "SELECT m.session_id, m.content, m.tool_calls FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE s.source != 'cron' AND m.timestamp <= ? AND (m.content LIKE ? OR m.tool_calls LIKE ?)",
                (cutoff, f"%{t}%", f"%{t}%")).fetchall()
            for sid, content, calls in rows:
                if rx.search(content or "") or rx.search(calls or ""):
                    counts[sid] = counts.get(sid, 0) + 1
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
            evs = data.pop("_events", [])
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
            for msg in evs:  # 台账落盘成功后才记事件；落盘失败则事件一条不写
                event(msg)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def queue_event(data, msg):
    data.setdefault("_events", []).append(msg)


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
        queue_event(data, f"reference {k}")
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
    queue_event(data, f"job {k} owner={owner}")
    return "new-job"


def scan(since=None, gh=gh_json, db=None, writeback=False):
    t0 = now()
    with ledger() as data:
        cur = parse(data["cursor"]) if data["cursor"] else None
        start = since or ((cur - OVERLAP) if cur else t0 - BOOTSTRAP)
        prs = fetch_merged_since(start, gh)
        res = {"new-job": [], "new-reference": [], "dup": []}
        new_keys = []
        for pr in sorted(prs, key=lambda p: p["merged_at"]):
            st = ingest(data, pr, gh, db)
            res[st].append(pr["number"])
            if st == "new-job":  # 按 repo#PR@merge_sha 精确记，同 PR 旧 merge 的单不被连带回写
                new_keys.append(key_of(pr))
        if not since:  # --since 是手动补扫，不动游标
            data["cursor"] = iso(t0)
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
        if extra.get("authorized_scope") is not None:
            extra["authorized_scope"] = extra["authorized_scope"].strip()
            if not extra["authorized_scope"]:
                raise SystemExit("--scope 不能为空白")
        if to == "executing" and not (extra.get("authorized_scope") or (j["authorized_scope"] or "").strip()):
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
        queue_event(data, f"{key} -> {to}")
        return dict(j), "ok"


# ---------- 门下回写（label） ----------

def want_labels(state):
    if state == "done":
        return set(), {"wait:agent", "wait:你拍", "wait:你读", "wait:闲", "agent:claimed", "agent:blocked"}
    if state == "blocked":
        return {"agent:blocked", "wait:你拍"}, {"wait:agent", "agent:claimed", "wait:你读"}
    add = {"wait:agent"} | ({"agent:claimed"} if state in ("executing", "verifying") else set())
    return add, {"wait:你拍", "wait:你读", "wait:闲", "agent:blocked"} | ({"agent:claimed"} - add)


LABEL_COLORS = {"wait:agent": "c5def5", "agent:claimed": "0e8a16", "agent:blocked": "b60205", "wait:你拍": "fbca04"}


def run_cmd(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)


def ensure_labels(names, gh=None, edit=None):
    """回写前确认 label 在仓里存在（gh api 只读列全量）；缺的用 gh label create 补，返回补建的名字。"""
    gh = gh or gh_json
    have = {l["name"] for l in gh_pages(f"repos/{REPO}/labels", gh)}
    missing = sorted(set(names) - have)
    for name in missing:
        (edit or run_cmd)(["gh", "label", "create", name, "-R", REPO, "--color", LABEL_COLORS.get(name, "ededed"),
                           "--description", "menxia approvals 台账回写"])
    return missing


def sync(key, gh=None, edit=None):
    gh = gh or gh_json
    with ledger() as data:
        j = dict(data["jobs"][key])
    add, rm = want_labels(j["state"])
    cur = set(labels_of(gh(f"repos/{REPO}/pulls/{j['pr']}")))
    to_add, to_rm = sorted(add - cur), sorted(rm & cur)
    if to_add:
        ensure_labels(to_add, gh, edit)
    if to_add or to_rm:
        cmd = ["gh", "pr", "edit", str(j["pr"]), "-R", REPO]
        for l in to_add:
            cmd += ["--add-label", l]
        for l in to_rm:
            cmd += ["--remove-label", l]
        (edit or run_cmd)(cmd)
    after = sorted(labels_of(gh(f"repos/{REPO}/pulls/{j['pr']}")))
    ok = add <= set(after) and not (rm & set(after))
    with ledger() as data:
        data["jobs"][key]["writeback"] = {"at": iso(now()), "state": j["state"], "labels": after, "verified": ok}
        queue_event(data, f"sync {key} state={j['state']} labels={after} verified={ok}")
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
            queue_event(data, f"{a.key} owner={a.owner_session}")
    elif a.cmd == "sync":
        print(json.dumps(sync(a.key), ensure_ascii=False))


def cli(argv=None):
    try:
        main(argv)
    except RuntimeError as e:  # gh 失败：不动游标，下一轮自然补扫
        print(f"approvals FAIL: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:  # gh label create / gh pr edit 写失败：台账状态已落，label 未回写
        err = (e.stderr or "").strip()[:300] if isinstance(e.stderr, str) else ""
        print(f"approvals WRITEBACK FAIL: {' '.join(map(str, e.cmd))} rc={e.returncode}: {err}；"
              f"台账已更新但门下 label 未回写，修好后跑 `approvals.py sync --key K` 补", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(cli())
