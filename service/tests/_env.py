"""测试环境：必须在 import dibao / common 之前加载（common 在 import 时读环境变量）。

AI_HOME_DATA = 临时 git 仓（分支 home-data）；AI_HOME_CACHE = 临时缓存目录；
worker 用 stub 脚本代替 apply_edits --worker（不碰真实 vault）。绝不指向真实 review-home-data。
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = tempfile.mkdtemp(prefix="wp09a-")
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, "cache")
REMOTE = os.path.join(ROOT, "remote.git")
STUB = os.path.join(ROOT, "stub_worker.py")
SNAP = os.path.join(CACHE, "snapshot-dashboard.json")

os.environ["AI_HOME_DATA"] = DATA
os.environ["AI_HOME_CACHE"] = CACHE
os.environ["DIBAO_CONFIG"] = os.path.join(ROOT, "no-such-config.toml")
os.environ["DIBAO_STATE_DIR"] = os.path.join(ROOT, "state")     # pushed.jsonl 不写真实 ~/.local/state
os.environ["AI_HOME_NTFY_DRYRUN"] = "1"                          # ntfy 兜底只打印，测试绝不外发
os.environ["DIBAO_WORKER"] = json.dumps([sys.executable, STUB])
os.environ["DIBAO_RECOLLECT"] = json.dumps([sys.executable, "-c", "pass"])

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "collectors"))

STUB_SRC = '''
import json, sys, time
edits = json.load(sys.stdin)
snap = json.load(open(%r))
out = []
for e in edits:
    if e["target"] == "p:slow":
        time.sleep(30)
    if e["target"] != "p:test":
        out.append({"id": e["id"], "state": "failed", "error": "找不到项目卡 " + e["target"], "changed": False})
        continue
    for k, v in (e.get("fields") or {}).items():
        snap["data"]["cards"][0][k] = v
    state = "needs_hermes" if (e.get("note") or "").strip() else "applied"
    out.append({"id": e["id"], "state": state, "summary": "stub 已落卡", "changed": True})
json.dump(snap, open(%r, "w"))
json.dump(out, sys.stdout)
''' % (SNAP, SNAP)


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout


def setup():
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    with open(STUB, "w") as f:
        f.write(STUB_SRC)
    with open(SNAP, "w") as f:
        json.dump({"source": "dashboard", "fetched": "2026-10-28T00:00:00Z",
                   "data": {"cards": [{"id": "p:test", "title": "测试卡", "next": "old"}]}}, f)
    sh("git", "init", "-q", "-b", "home-data", DATA)
    sh("git", "-C", DATA, "config", "user.email", "t@example.com")
    sh("git", "-C", DATA, "config", "user.name", "t")
    with open(os.path.join(DATA, "README.md"), "w") as f:
        f.write("fake home-data\n")
    sh("git", "-C", DATA, "add", "README.md")
    sh("git", "-C", DATA, "commit", "-q", "-m", "init")


setup()
