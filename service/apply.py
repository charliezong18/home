"""包 apply_edits 的子进程调用：主进程永不直接 open vault 路径，卡片读写全在 worker 子进程里，硬超时。"""
import json
import os
import subprocess
import sys

COLLECTORS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collectors")
DEFAULT_WORKER = [sys.executable, os.path.join(COLLECTORS, "apply_edits.py"), "--worker"]
DEFAULT_RECOLLECT = [sys.executable, os.path.join(COLLECTORS, "collect_dashboard.py")]


def run_worker(cmd, edits, timeout, log=print):
    """stdin 喂 pending 编辑数组，返回 [{id,state,summary|error,changed}]；超时 / 异常返回 None（不记状态，下轮再试）。"""
    try:
        r = subprocess.run(cmd, input=json.dumps(edits, ensure_ascii=False),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"apply: worker 超过 {timeout}s 被杀，{len(edits)} 条留到下轮")
        return None
    if r.returncode != 0:
        log(f"apply: worker rc={r.returncode}: {r.stderr.strip()[:300]}")
        return None
    try:
        out = json.loads(r.stdout)
    except ValueError:
        log(f"apply: worker 输出不是 JSON: {r.stdout.strip()[:200]}")
        return None
    return out if isinstance(out, list) else None


def recollect(cmd, timeout, log=print):
    """卡改了：重采 dashboard 快照，build 才拿得到新值。失败只记日志。"""
    try:
        rc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        log(f"apply: 重采 dashboard 超过 {timeout}s")
        return False
    if rc != 0:
        log(f"apply: 重采 dashboard rc={rc}")
    return rc == 0
