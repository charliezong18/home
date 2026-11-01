"""Google Tasks 勾完成：消费 home-data 的 todo-actions.jsonl（网页 / iOS 经 GitHub 追加，或本机 /v1/todos 端点）。

一行（客户端写入的首行）：
  {"id":"<uuid>","ts":"<ISO8601>","task_id":"<Google Tasks id>","op":"complete"|"undo",
   "title":"<当时标题，仅日志用>","from":"web"|"ios"|…,"state":"pending"}
dibao 处理后追加状态行 {"id","state":"done"|"failed","ts","result"|"error"}。

只允许 complete / undo：不建、不删、不改标题、不动 notes。
幂等：同 id 只执行一次——文件里出现过 done/failed 行（终态粘住，客户端重发同 id 的 pending 行也不重放），
或本机账本 todo-actions-done.jsonl（DIBAO_STATE_DIR）记过，都算已处理。
gog 失败 → 先只读 `gog tasks get` 看任务是否已是目标状态（是则算成功），否则行标 failed 并保留原因，不自动重试。
gog 调用经可注入的 runner（缺省 subprocess.run），测试全部用假 gog。
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "collectors"))

from collect_gtasks import GOG, LIST_ID  # noqa: E402
import ledger  # noqa: E402

OPS = {"complete": "done", "undo": "undo"}                 # op → gog 子命令（只此两种）
TARGET_STATUS = {"complete": "completed", "undo": "needsAction"}
TERMINAL = {"done", "failed"}
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,199}$")   # 不以 - 开头，免得被 gog 当成参数
CALL_TIMEOUT = 30
TITLE_MAX = 200


def state_path():
    d = os.environ.get("DIBAO_STATE_DIR") or os.path.expanduser("~/.local/state/dibao")
    return os.path.join(d, "todo-actions-done.jsonl")


def validate(a):
    """首行 → 错误文本；合法返回 None。"""
    if a.get("op") not in OPS:
        return f"op={a.get('op')!r} 不被允许（只接受 {sorted(OPS)}）"
    if not isinstance(a.get("task_id"), str) or not TASK_ID_RE.match(a["task_id"]):
        return f"task_id 不合法：{str(a.get('task_id'))[:60]!r}"
    return None


def new_action(task_id, op, title, frm, ts, aid=None):
    """本机端点用：生成首行；不合法抛 ValueError。"""
    import uuid
    a = {"id": str(aid or uuid.uuid4()), "ts": ts, "task_id": task_id, "op": op,
         "title": str(title or "")[:TITLE_MAX], "from": frm, "state": "pending"}
    err = validate(a)
    if err:
        raise ValueError(err)
    return a


def read_records(path):
    try:
        with open(path, encoding="utf-8") as f:
            return ledger.parse_lines(f.read())
    except OSError:
        return []


def local_done(path=None):
    return {r["id"] for r in read_records(path or state_path())}


def summarize(records, done_ids=()):
    """records（文件顺序）→ (首行按出现顺序, {id: 最新终态行}, 待处理首行)。"""
    first, final = {}, {}
    for r in records:
        first.setdefault(r["id"], r)
        if r.get("state") in TERMINAL and r["id"] not in final:
            final[r["id"]] = r                                 # 第一条终态行为准，之后的行不能把它改回 pending
    todo = [a for i, a in first.items() if a.get("state") == "pending" and i not in final and i not in done_ids]
    return first, final, todo


def _gog(runner, args, timeout=CALL_TIMEOUT):
    """→ (rc, stdout, 错误文本)；超时 / 起不来 rc=None。"""
    try:
        r = runner([GOG, "tasks", *args, "--no-input", "--color=never"], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", f"gog 超时 {timeout}s"
    except OSError as e:
        return None, "", f"gog 起不来：{e}"
    return r.returncode, r.stdout or "", (r.stderr or "").strip()[:200]


def current_status(task_id, runner=subprocess.run, list_id=LIST_ID):
    """只读 `gog tasks get` → "completed" / "needsAction" / None（读不到）。"""
    rc, out, _ = _gog(runner, ["get", list_id, task_id, "--json"])
    if rc != 0:
        return None
    try:
        obj = json.loads(out or "null")
    except ValueError:
        return None
    if isinstance(obj, dict) and isinstance(obj.get("task"), dict):
        obj = obj["task"]
    return obj.get("status") if isinstance(obj, dict) else None


def execute(a, runner=subprocess.run, list_id=LIST_ID):
    """执行一条首行 → (state, 结果或错误)。不重试。"""
    err = validate(a)
    if err:
        return "failed", err
    op, tid = a["op"], a["task_id"]
    rc, _, msg = _gog(runner, [OPS[op], list_id, tid])
    if rc == 0:
        return "done", "ok"
    if current_status(tid, runner, list_id) == TARGET_STATUS[op]:
        return "done", "already"                           # 已是目标状态 = 成功
    return "failed", (f"gog {OPS[op]} rc={rc}: {msg}" if rc is not None else msg)[:200] or f"gog {OPS[op]} 失败"


def state_line(aid, state, detail, ts):
    line = {"id": aid, "state": state, "ts": ts}
    line["result" if state == "done" else "error"] = detail
    return line


def remember(line, path=None):
    """本机账本：执行完立刻记（不在 git 里，防 home-data 提交丢失时重放）。"""
    path = path or state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ledger.append(path, line)
