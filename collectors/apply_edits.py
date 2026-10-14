#!/usr/bin/env python3
"""编辑流落盘器：主页弹窗「编辑 → 保存」写进 home-data 的 edits.jsonl，本脚本把它落到 vault 项目卡。

账本 edits.jsonl 只追加：网页写 state=pending 的原始记录，本脚本追加同 id 的状态行（后者生效）：
  pending → applied        字段/里程碑已落卡，没有附言
  pending → needs_hermes   字段部分已落卡（或没有字段改动），附言 note 留给 Hermes 派 Claude 处理
  pending → failed         校验不过（目标卡不存在、取值非法、里程碑文本对不上），整条不落盘
  needs_hermes → done      Hermes 处理完附言后用 --mark-done 追加
本脚本绝不调 claude。读写 vault 放在子进程里，30 秒硬超时；超时整轮跳过、不记状态，下轮再试。

用法：
  apply_edits.py                       落盘所有 pending（launchd 每 5 分钟，见 run_edits.sh）
  apply_edits.py --list-pending-notes  打印 needs_hermes 且未 done 的记录（JSON 数组，Hermes cron 用）
  apply_edits.py --mark-done <id> [--result 文本]
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA_DIR, data_lock, git, log, now_iso, sync_down, today_local  # noqa: E402

LEDGER = os.path.join(DATA_DIR, "edits.jsonl")
TIMEOUT = 30
FIELDS = {"tier", "status", "next", "deadline"}
STATUS_VALS = {"active", "waiting", "you", "ice", "done"}
TIER_VALS = {"now", "build", "learn", "ice"}
HERE = os.path.dirname(os.path.abspath(__file__))


# ---------- 账本 ----------

def load_ledger():
    """同 id 合并（后者覆盖），保留首次出现的顺序。坏行跳过并告警，不拖垮整本账。"""
    recs = {}
    if not os.path.exists(LEDGER):
        return recs
    with open(LEDGER, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                log(f"edits.jsonl 第 {n} 行不是合法 JSON，跳过")
                continue
            if not isinstance(e, dict) or not e.get("id"):
                continue
            prev = recs.get(e["id"], {})
            recs[e["id"]] = {**prev, **e, "created": prev.get("created", e.get("ts"))}   # ts = 最后一次状态变化；created = 首行
    return recs


def append_lines(lines, message):
    """追加状态行并推送。远端（网页 PUT）同时在追加时 push 会被拒：撤掉本地这次 commit、重新拉、重新追加，最多 3 次。"""
    if not lines:
        return
    for attempt in range(3):
        if git("status", "--porcelain").stdout.strip():
            raise RuntimeError("home-data 工作树不干净，不敢动：" + git("status", "--porcelain").stdout.strip()[:200])
        sync_down()
        with open(LEDGER, "a", encoding="utf-8") as f:
            for e in lines:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        git("add", "--", "edits.jsonl")
        git("commit", "--quiet", "-m", message)
        try:
            git("push", "--quiet", "origin", "HEAD:home-data", timeout=90)
            return
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            log(f"push 失败（第 {attempt + 1} 次），撤回本地 commit 重试：{e}")
            git("reset", "--hard", "--quiet", "HEAD~1")
    raise RuntimeError("edits.jsonl 推送 3 次都失败")


# ---------- 卡片（子进程里跑） ----------

def yaml_scalar(v):
    """next 之类自由文本：含 YAML 敏感字符就加双引号，否则原样（与现有卡的写法一致）。"""
    if v == "" or (not re.search(r": | #", v) and not re.match(r"^[-?:,\[\]{}#&*!|>'\"%@`]", v)):
        return v
    return json.dumps(v, ensure_ascii=False)


def validate(edit):
    fields = edit.get("fields") or {}
    if not isinstance(fields, dict):
        return "fields 不是对象"
    for k, v in fields.items():
        if k not in FIELDS:
            return f"字段 {k} 不在白名单"
        if not isinstance(v, str):
            return f"字段 {k} 不是字符串"
        if k == "status" and v not in STATUS_VALS:
            return f"status={v} 非法"
        if k == "tier" and v not in TIER_VALS:
            return f"tier={v} 非法"
        if k == "deadline" and v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return f"deadline={v} 不是 YYYY-MM-DD"
        if k == "next" and ("\n" in v or len(v) > 300):
            return "next 必须单行且 ≤300 字"
    ms = edit.get("milestones") or []
    if not isinstance(ms, list) or any(not isinstance(m, dict) or not str(m.get("text", "")).strip() for m in ms):
        return "milestones 格式不对"
    return None


def section_range(lines, name, start):
    """## name 小节的 (标题行, 结束行) 下标；没有返回 None。"""
    for i in range(start, len(lines)):
        if re.match(rf"^##\s+{re.escape(name)}\s*$", lines[i]):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("## "):
                j += 1
            return i, j
    return None


def apply_card(path, edit, today):
    """返回 (changed, summary)。任何校验失败抛 ValueError，此时文件一个字节都没动。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.split("\n")
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("卡片没有 frontmatter")
    fm_end = lines.index("---", 1)
    parts = []

    for k, v in (edit.get("fields") or {}).items():
        v = v.strip()
        idx = next((i for i in range(1, fm_end) if lines[i].startswith(k + ":")), None)
        old = lines[idx].split(":", 1)[1].strip().strip('"').strip("'") if idx is not None else ""
        if old == v:
            continue
        new_line = f"{k}: {yaml_scalar(v)}".rstrip()
        if idx is None:
            lines.insert(fm_end, new_line)
            fm_end += 1
        else:
            lines[idx] = new_line
        parts.append(f"{k} 改为「{v}」" if k == "next" else f"{k} {old or '空'}→{v or '空'}")

    ms_edits = edit.get("milestones") or []
    if ms_edits:
        rng = section_range(lines, "里程碑", fm_end)
        if not rng:
            raise ValueError("卡片没有 ## 里程碑")
        for m in ms_edits:
            want = str(m["text"]).strip()
            hits = []
            for i in range(rng[0] + 1, rng[1]):
                cm = re.match(r"^(\s*[-*]\s+\[)( |x|X)(\]\s*)(.*)$", lines[i])
                if cm and cm.group(4).strip() == want:
                    hits.append((i, cm))
            if len(hits) != 1:
                raise ValueError(f"里程碑「{want[:40]}」命中 {len(hits)} 行≠1")
            i, cm = hits[0]
            mark = "x" if m.get("done") else " "
            if cm.group(2).lower() == mark:
                continue
            lines[i] = cm.group(1) + mark + cm.group(3) + cm.group(4)
            parts.append(f"{'勾选' if mark == 'x' else '取消勾选'}「{want}」")

    if not parts:
        return False, "无实际变化"
    summary = "；".join(parts)
    if (edit.get("note") or "").strip():
        summary += "；附言已转 Hermes"
    entry = f"- {today} 主页编辑：{summary}"
    for i in range(1, fm_end):
        if lines[i].startswith("updated:"):
            lines[i] = f"updated: {today}"
            break
    else:
        lines.insert(fm_end, f"updated: {today}")
        fm_end += 1
    if entry not in lines:   # 幂等：上一轮落了卡却没推上账本时，不重复记日志
        rng = section_range(lines, "日志", fm_end)
        if rng:
            j = rng[1]
            while j > rng[0] + 1 and not lines[j - 1].strip():
                j -= 1
            lines.insert(j, entry)
        else:
            while lines and lines[-1] == "":
                lines.pop()
            lines += ["", "## 日志", entry, ""]
    new = "\n".join(lines)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(tmp, path)
    return True, summary


def worker():
    """stdin：pending 编辑数组 → stdout：[{id, state, summary|error, changed}]。"""
    import glob
    import collect_dashboard as cd
    edits = json.load(sys.stdin)
    cards = []
    for p in sorted(glob.glob(os.path.join(cd.VAULT_DASH, "*.md"))):
        if os.path.basename(p) != "README.md":
            cards.append(cd.parse_card(p))
    cd.dedupe_ids(cards)
    by_id = {c["id"]: os.path.join(os.path.dirname(cd.VAULT_DASH), c["ref"]) for c in cards}
    today = today_local()
    out = []
    for e in edits:
        has_note = bool((e.get("note") or "").strip())
        try:
            err = validate(e)
            if err:
                raise ValueError(err)
            path = by_id.get(e.get("target"))
            if not path:
                raise ValueError(f"找不到项目卡 {e.get('target')}")
            changed, summary = apply_card(path, e, today)
            out.append({"id": e["id"], "state": "needs_hermes" if has_note else "applied",
                        "summary": summary, "changed": changed})
        except (ValueError, OSError) as ex:
            out.append({"id": e["id"], "state": "failed", "error": str(ex)[:300], "changed": False})
    json.dump(out, sys.stdout, ensure_ascii=False)


# ---------- 子命令 ----------

def cmd_apply():
    with data_lock():
        sync_down()
        pending = [e for e in load_ledger().values() if e.get("state") == "pending"]
        if not pending:
            log("edits: 没有 pending")
            return 0
        try:
            r = subprocess.run([sys.executable, os.path.abspath(__file__), "--worker"],
                               input=json.dumps(pending, ensure_ascii=False),
                               capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f"edits: 读写 vault 超过 {TIMEOUT}s，本轮跳过（{len(pending)} 条留到下轮）")
            return 2
        if r.returncode != 0:
            log(f"edits: worker rc={r.returncode}: {r.stderr.strip()[:300]}，本轮跳过")
            return 2
        results = json.loads(r.stdout)
        ts = now_iso()
        lines = []
        for x in results:
            line = {"id": x["id"], "state": x["state"], "ts": ts}
            if x.get("summary"):
                line["summary"] = x["summary"]
            if x.get("error"):
                line["error"] = x["error"]
            lines.append(line)
            log(f"edits: {x['id']} → {x['state']} {x.get('summary') or x.get('error') or ''}")
        append_lines(lines, f"edits: apply {len(lines)}")
    if any(x["changed"] for x in results):   # 卡改了：立刻重采一次 dashboard，build 才拿得到新值
        rc = subprocess.run([sys.executable, os.path.join(HERE, "collect_dashboard.py")]).returncode
        subprocess.run([sys.executable, os.path.join(HERE, "status.py"), "dashboard", "ok" if rc == 0 else "fail",
                        *([] if rc == 0 else ["apply_edits 触发的重采失败"])])
    return 0


def cmd_list():
    with data_lock():
        sync_down()
        recs = [e for e in load_ledger().values() if e.get("state") == "needs_hermes"]
    print(json.dumps(recs, ensure_ascii=False, indent=2))
    return 0


def cmd_mark_done(eid, result):
    with data_lock():
        sync_down()
        rec = load_ledger().get(eid)
        if not rec:
            print(f"没有这条编辑：{eid}", file=sys.stderr)
            return 1
        if rec.get("state") == "done":
            print(f"{eid} 已经是 done，不重复记")
            return 0
        line = {"id": eid, "state": "done", "ts": now_iso()}
        if result:
            line["result"] = result
        append_lines([line], f"edits: done {eid}")
    print(f"{eid} → done")
    return 0


def main():
    if "--worker" in sys.argv:
        worker()
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list-pending-notes", action="store_true")
    ap.add_argument("--mark-done", metavar="ID")
    ap.add_argument("--result", default="")
    a = ap.parse_args()
    if a.list_pending_notes:
        return cmd_list()
    if a.mark_done:
        return cmd_mark_done(a.mark_done, a.result)
    return cmd_apply()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"edits FAIL: {e}", file=sys.stderr)
        sys.exit(1)
