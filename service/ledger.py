"""账本（jsonl，只追加，同 id 后者覆盖）的纯函数部分。无 git、无锁、无网络，便于单测。"""
import json
import os
import uuid

EDIT_FROM = {"hermes", "claude"}          # HTTP 入口允许的来源；web/ios 走 GitHub 不经此处
READING_STATUS = {"new", "reading", "done", "dropped"}


def parse_lines(text):
    """文本 → 记录列表；坏行、非对象行跳过。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("id"):
            out.append(e)
    return out


def merge(records):
    """同 id 合并（后者覆盖），保留首次出现顺序；created = 首行 ts。与 apply_edits.load_ledger 同语义。"""
    recs = {}
    for e in records:
        prev = recs.get(e["id"], {})
        recs[e["id"]] = {**prev, **e, "created": prev.get("created", e.get("ts"))}
    return recs


def read_ledger(path):
    try:
        with open(path, encoding="utf-8") as f:
            return merge(parse_lines(f.read()))
    except OSError:
        return {}


def append(path, obj):
    """追加一行；文件末尾没换行（网页 PUT 可能留下）先补一个。"""
    need_nl = False
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell():
                f.seek(-1, os.SEEK_END)
                need_nl = f.read(1) != b"\n"
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        if need_nl:
            f.write("\n")
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def pending_edits(recs):
    return [e for e in recs.values() if e.get("state") == "pending"]


def notes_pending(recs):
    return [e for e in recs.values() if e.get("state") == "needs_hermes"]


def normalize_edit(body, ts):
    """HTTP body → 待追加的 Edit 首行。校验字段白名单交给 apply_edits.validate，这里只管形状。"""
    if not isinstance(body, dict):
        raise ValueError("body 不是对象")
    if not str(body.get("target", "")).strip():
        raise ValueError("缺 target")
    frm = body.get("from", "claude")
    if frm not in EDIT_FROM:
        raise ValueError(f"from={frm} 不被允许（只接受 {sorted(EDIT_FROM)}）")
    if not (body.get("fields") or body.get("milestones") or str(body.get("note", "")).strip()):
        raise ValueError("fields / milestones / note 至少要有一项")
    e = {k: body[k] for k in ("target", "fields", "milestones", "note") if k in body}
    e["id"] = str(body.get("id") or f"e:{ts.replace('-', '').replace(':', '')}-{uuid.uuid4().hex[:4]}")
    e["ts"] = body.get("ts") or ts
    e["state"] = "pending"
    e["from"] = frm
    return e


def find_reading(recs, key):
    """按账本 id 或展示 rid（r:…）找读物。"""
    if key in recs:
        return recs[key]
    for r in recs.values():
        if key == "r:" + r["id"].removeprefix("link-"):
            return r
    return None
