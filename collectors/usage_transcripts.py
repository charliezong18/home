#!/usr/bin/env python3
"""worker transcript jsonl → 重复抓取 / 重复整读统计（R-REFETCH / R-REREAD 的数据源）。纯标准库，只读。

隐私硬约束：只取 tool_use 的工具名和参数里的 url / file_path（以及是否带 offset/limit）；
不取 tool_result、assistant 文字、user 文字，也不把 url / path 写进 usage.json（只出计数）。

transcript 位置：runs/<task>.json 的 session_id → ~/.claude/projects/*/<session_id>.jsonl（按 session_id 找，找不到就当没有）。
只数 type=="assistant" 的行（api-request-blob 等行里有同一 tool_use 的副本，不重复数）。
"""
import glob
import json
import os
from collections import Counter

PROJECTS = os.environ.get("USAGE_CLAUDE_PROJECTS", os.path.expanduser("~/.claude/projects"))
URL_TOOLS = {"WebFetch": "url", "mcp__plugin_context-mode_context-mode__ctx_fetch_and_index": "url"}
READ_TOOL = "Read"
REFETCH_MIN = 2     # 同一任务同 URL ≥2 次算重复抓取（超出第 1 次的部分计浪费）
REREAD_MIN = 3      # 同一任务同文件整读 ≥3 次算重复整读


def find_transcript(session_id, projects=None):
    """按 session_id 找 transcript；找不到返回 None。session_id 只允许 uuid 字符，防路径穿越。"""
    if not session_id or not all(c.isalnum() or c == "-" for c in str(session_id)):
        return None
    hits = glob.glob(os.path.join(projects or PROJECTS, "*", f"{session_id}.jsonl"))
    return sorted(hits)[0] if hits else None


def tool_calls(path):
    """yield (kind, key)：kind = 'url'（抓取）| 'read'（整读：没有 offset/limit）。其余工具与带 offset/limit 的 Read 忽略。"""
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            if '"tool_use"' not in line:               # 便宜的预筛：绝大多数行不含 tool_use
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict) or o.get("type") != "assistant":
                continue
            content = (o.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name"), b.get("input")
                if not isinstance(inp, dict):
                    continue
                if name in URL_TOOLS and isinstance(inp.get(URL_TOOLS[name]), str):
                    yield "url", inp[URL_TOOLS[name]]
                elif name == READ_TOOL and isinstance(inp.get("file_path"), str) \
                        and inp.get("offset") is None and inp.get("limit") is None:
                    yield "read", inp["file_path"]


def scan_transcript(path):
    """→ {"refetch": 超出首次的重复抓取次数, "reread": 超出首次的重复整读次数}（只数达到阈值的 URL / 文件）。"""
    urls, reads = Counter(), Counter()
    for kind, key in tool_calls(path):
        (urls if kind == "url" else reads)[key] += 1
    return {"refetch": sum(n - 1 for n in urls.values() if n >= REFETCH_MIN),
            "reread": sum(n - 1 for n in reads.values() if n >= REREAD_MIN)}


def scan_task(task, projects=None):
    """task 聚合对象（带 session_id）→ scan_transcript 结果；没有 session_id / 找不到 transcript → None。"""
    path = find_transcript(task.get("session_id"), projects)
    return scan_transcript(path) if path else None
