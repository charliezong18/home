"""AI 主页采集器共用：路径、锁、git、状态读写。仅标准库。"""
import datetime as _dt
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("AI_HOME_DATA", os.path.join(HOME, "Developer/review-home-data"))
CACHE_DIR = os.environ.get("AI_HOME_CACHE", os.path.join(HOME, "Library/Caches/ai-home"))
LOCK_FILE = os.path.join(CACHE_DIR, "git.lock")
REPO = "charliezong18/review"
BRANCH = "home-data"
NTFY_URL = "https://ntfy.sh/charlie-claude-notify-6d4720"
SOURCES = ["menxia", "dashboard", "readings"]

# launchd 下 PATH 很短，gh/git 在 Homebrew 里
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "/usr/bin:/bin")


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_local():
    return _dt.date.today().isoformat()


def log(msg):
    print(f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def ensure_dirs():
    os.makedirs(CACHE_DIR, exist_ok=True)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def snapshot_path(source):
    return os.path.join(CACHE_DIR, f"snapshot-{source}.json")


def save_snapshot(source, data):
    ensure_dirs()
    write_json(snapshot_path(source), {"source": source, "fetched": now_iso(), "data": data})


def run(cmd, cwd=None, timeout=60, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> rc={r.returncode}: {r.stderr.strip()[:300]}")
    return r


def git(*args, timeout=60, check=True):
    return run(["git", "-C", DATA_DIR, *args], timeout=timeout, check=check)


@contextmanager
def data_lock():
    """add_link 与 run_all 共用一把锁，防止同时 pull/commit/push。"""
    ensure_dirs()
    with open(LOCK_FILE, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def sync_down():
    """拉远端 home-data（远端还没有该分支时跳过）。"""
    r = git("ls-remote", "--heads", "origin", BRANCH, check=False)
    if BRANCH in (r.stdout or ""):
        git("pull", "--rebase", "--quiet", "origin", BRANCH)


def commit_and_push(paths, message):
    """只 add 指定文件；无变更返回 False。"""
    git("add", "--", *paths)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git("commit", "--quiet", "-m", message)
    git("push", "--quiet", "origin", f"HEAD:{BRANCH}", timeout=90)
    return True


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)
