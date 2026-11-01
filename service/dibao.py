#!/usr/bin/env python3
"""dibao：Mac mini 本地服务（#187 A 期）。标准库 HTTP，只绑 127.0.0.1。

真源 = 本地 home-data 工作树（common.DATA_DIR）。本进程是本机唯一写者：
  · 每 sync_down_secs 秒 git pull --rebase → 发现 pending 编辑 → 子进程落卡 → 重算 home.json
  · POST /v1/edits 等本机调用：追加账本 → 立刻落卡 → 重算
  · 本地提交后 push_debounce_secs 防抖，再 push 回 GitHub
用法：dibao.py [--no-push] [--once-sync] [--selftest]；端口 DIBAO_PORT / config.toml（缺省 8766）。
"""
import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "collectors"))

import add_link  # noqa: E402
import apply  # noqa: E402
import apply_edits  # noqa: E402
import build_home  # noqa: E402
import config  # noqa: E402
import ledger  # noqa: E402
import apns  # noqa: E402
import events  # noqa: E402
import todo_actions  # noqa: E402
import dibao_sched as sched  # noqa: E402  (renamed: avoid shadowing stdlib sched)
from common import BRANCH, DATA_DIR, data_lock, git, now_iso, read_json, sync_down  # noqa: E402

VERSION = "0.1.0"
EDITS = os.path.join(DATA_DIR, "edits.jsonl")
READINGS = os.path.join(DATA_DIR, "readings.jsonl")
HOME_JSON = os.path.join(DATA_DIR, "home.json")
STATUS_JSON = os.path.join(DATA_DIR, "status.json")
DEVICES = os.path.join(DATA_DIR, "devices.jsonl")
TODO_ACTIONS = os.path.join(DATA_DIR, "todo-actions.jsonl")
DUE_SCAN_AT = (7, 30)                    # 本地时间；晚于此启动则当天补扫一次（键里带日期，不会重复推）
MAX_BODY = 64 * 1024
log = logging.getLogger("dibao")


class Service:
    def __init__(self, cfg, push=True):
        self.cfg, self.push = cfg, push
        self.started = time.time()
        self.svc = threading.RLock()            # 本进程内所有写操作串行；data_lock 只在单步 git/文件操作里短持
        self.stop = threading.Event()
        self.last_sync_down = self.last_push = None
        self.last_sync_ok = self.last_sync_error = None    # 最近一轮 sync 的成败（None = 还没跑过）
        self.last_push_ok = self.last_push_error = None
        self.pusher = sched.Pusher(self.do_push, cfg.push_debounce_secs)
        self.sched = sched.Scheduler(self)
        self.notifier = events.Notifier(apns.Apns(cfg.apns), DEVICES, on_deactivate=self._deactivate)
        self.due_done = None
        self.todo_runner = subprocess.run                 # gog 子进程入口；测试换成假 gog
        self.recollect_gtasks = lambda: self.sched.run_source("gtasks")  # 勾完后立刻重采 gtasks；测试可换
        # build_home 直接 import 复用，但它的 sync_down / commit_and_push 换成本服务的（只本地提交，push 走防抖线程）
        build_home.sync_down = lambda: None
        build_home.commit_and_push = self._commit

    # ---------- git ----------
    def _head(self):
        return git("rev-parse", "HEAD", check=False).stdout.strip()

    def _commit(self, paths, message):
        """调用方已持 data_lock（build_home.main 自带；本服务其余路径显式持）。"""
        git("add", "--", *paths)
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        git("commit", "--quiet", "-m", message)
        if self.push:
            self.pusher.mark()
        return True

    def prepare_repo(self):
        """让 pull --rebase 在两边都追加 jsonl 末尾时不冲突（union），生成物冲突取本地（马上重算）。"""
        info = os.path.join(DATA_DIR, ".git", "info")
        if os.path.isdir(info):
            with open(os.path.join(info, "attributes"), "w") as f:
                f.write("*.jsonl merge=union\nhome.json merge=ours\nstatus.json merge=ours\n")
            git("config", "merge.ours.driver", "true", check=False)

    # ---------- 落卡 + 重算 ----------
    def rebuild(self):
        rc = build_home.main()
        self.fire(lambda: self.notifier.scan_home(read_json(HOME_JSON, {}) or {}))
        return rc

    # ---------- 推送（APNs，ntfy 兜底）----------
    def fire(self, fn):
        """推送走独立线程：不占 svc 锁，也不让 curl 超时拖慢写接口。异常只记日志。"""
        def run():
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("推送异常")
        threading.Thread(target=run, name="notify", daemon=True).start()

    def _deactivate(self, token, reason):
        """APNs 报 410 / BadDeviceToken：往 devices.jsonl 追加 active:false 并本地提交（不持锁地调用）。"""
        with self.svc:
            with data_lock():
                ledger.append(DEVICES, {"token": token, "active": False, "ts": now_iso(), "reason": reason})
                self._commit(["devices.jsonl"], f"devices: deactivate {apns.tail(token)}")

    def due_tick(self):
        t = time.localtime()
        today = time.strftime("%Y-%m-%d", t)
        if (t.tm_hour, t.tm_min) >= DUE_SCAN_AT and self.due_done != today:
            self.due_done = today
            self.notifier.scan_due(read_json(HOME_JSON, {}) or {}, today)

    def push_test(self, body):
        body = body or {}
        res = self.notifier.send_test(str(body.get("title") or "测试"), str(body.get("body") or "门下推送"),
                                      str(body.get("url") or "menxia://home"))
        return (200 if res in ("apns", "ntfy") else 502), {"result": res, "apns": self.notifier.health()}

    def process_pending(self):
        """落所有 pending 编辑。返回 {id: 状态行}；超时/worker 异常返回 None（账本不动，下轮再试）。"""
        with data_lock():
            pending = ledger.pending_edits(ledger.read_ledger(EDITS))
        if not pending:
            return {}
        results = apply.run_worker(self.cfg.worker_cmd, pending, self.cfg.worker_timeout, log.warning)
        if results is None:
            return None
        ts, lines = now_iso(), {}
        for x in results:
            line = {"id": x["id"], "state": x["state"], "ts": ts}
            for k in ("summary", "error"):
                if x.get(k):
                    line[k] = x[k]
            lines[x["id"]] = line
            log.info("edit %s → %s %s", x["id"], x["state"], x.get("summary") or x.get("error") or "")
        with data_lock():
            for line in lines.values():
                ledger.append(EDITS, line)
            self._commit(["edits.jsonl"], f"edits: apply {len(lines)}")
        if any(x.get("changed") for x in results):
            apply.recollect(self.cfg.recollect_cmd, self.cfg.worker_timeout, log.warning)
        self.rebuild()
        return lines

    def process_todos(self):
        """执行 todo-actions.jsonl 里未处理的勾完成 / 撤销。返回 {id: 状态行}。每条只执行一次，失败不重试。"""
        with self.svc:
            with data_lock():
                _, _, todo = todo_actions.summarize(todo_actions.read_records(TODO_ACTIONS), todo_actions.local_done())
            if not todo:
                return {}
            lines = {}
            for a in todo:                                   # 文件顺序：先勾后撤销 = 最终未完成
                state, detail = todo_actions.execute(a, self.todo_runner)
                line = todo_actions.state_line(a["id"], state, detail, now_iso())
                todo_actions.remember(line)
                lines[a["id"]] = line
                log.info("todo %s %s %s → %s %s", a["id"], a.get("op"), a.get("task_id"), state, detail)
            with data_lock():
                for line in lines.values():
                    ledger.append(TODO_ACTIONS, line)
                self._commit(["todo-actions.jsonl"], f"todos: apply {len(lines)}")
            if any(x["state"] == "done" for x in lines.values()):
                try:
                    self.recollect_gtasks()
                except Exception:  # noqa: BLE001
                    log.exception("勾完后重采 gtasks 失败")
            self.rebuild()
            return lines

    def post_todo(self, task_id, op, body):
        """本机端点 POST /v1/todos/<id>/complete|undo：追加首行 → 立刻执行。body 可带 id（幂等键）/ title / from。"""
        body = body or {}
        frm = body.get("from", "claude")
        if frm not in ledger.EDIT_FROM:
            raise ValueError(f"from={frm} 不被允许（只接受 {sorted(ledger.EDIT_FROM)}）")
        a = todo_actions.new_action(task_id, op, body.get("title"), frm, now_iso(), body.get("id"))
        with self.svc:
            with data_lock():
                first, final, _ = todo_actions.summarize(todo_actions.read_records(TODO_ACTIONS))
                if a["id"] in first and (first[a["id"]].get("task_id"), first[a["id"]].get("op")) != (task_id, op):
                    raise ValueError(f"id {a['id']} 已用于别的任务 / 操作")
                if a["id"] not in first:
                    ledger.append(TODO_ACTIONS, a)
                    self._commit(["todo-actions.jsonl"], f"todos: + {op} {a['id']}")
            line = final.get(a["id"]) or self.process_todos().get(a["id"])
        if not line:
            return 202, {"action": {**a, "state": "pending"}}
        out = {k: v for k, v in {**a, **line}.items() if k in ("id", "task_id", "op", "state", "result", "error")}
        return (200 if line["state"] == "done" else 502), {"action": out}

    def sync_cycle(self):
        with self.svc:
            with data_lock():
                if git("status", "--porcelain").stdout.strip():
                    log.error("home-data 工作树不干净，本轮不拉")
                    self.last_sync_ok, self.last_sync_error = False, "home-data 工作树不干净，本轮不拉"
                    return
                before = self._head()
                try:
                    sync_down()
                    self.last_sync_ok, self.last_sync_error = True, None
                except Exception as e:  # noqa: BLE001 — 断网不拖垮循环
                    log.warning("sync_down 失败：%s", str(e)[:200])
                    self.last_sync_ok, self.last_sync_error = False, str(e)[:200]
                after = self._head()
            self.last_sync_down = now_iso()
            done = self.process_pending()
            try:
                done = self.process_todos() or done          # 网页 / iOS 经 GitHub 追加的勾完成；跟 sync 同频（≤ sync_down_secs）
            except Exception:  # noqa: BLE001
                log.exception("todo-actions 处理异常")
            if after != before and not done:
                self.rebuild()
            if self.push and git("rev-list", "--count", f"origin/{BRANCH}..HEAD", check=False).stdout.strip() not in ("", "0"):
                self.pusher.mark()

    def do_push(self):
        with data_lock():
            r = git("push", "--quiet", "origin", f"HEAD:{BRANCH}", timeout=90, check=False)
        if r.returncode == 0:
            self.last_push = now_iso()
            self.last_push_ok, self.last_push_error = True, None
            return True
        log.warning("push 失败（防抖线程会重试）：%s", r.stderr.strip()[:200])
        self.last_push_ok, self.last_push_error = False, r.stderr.strip()[:200]
        return False

    # ---------- 后台线程 ----------
    def start_threads(self):
        def sync_loop():
            while not self.stop.wait(self.cfg.sync_down_secs):
                try:
                    self.sync_cycle()
                except Exception:  # noqa: BLE001
                    log.exception("sync_cycle 异常")

        threading.Thread(target=sync_loop, name="sync", daemon=True).start()
        threading.Thread(target=self.sched.loop, args=(self.stop, self.cfg.collect_secs), name="collect", daemon=True).start()

        def due_loop():
            while not self.stop.wait(60):
                try:
                    self.due_tick()
                except Exception:  # noqa: BLE001
                    log.exception("到期扫描异常")

        threading.Thread(target=due_loop, name="due", daemon=True).start()
        if self.push:
            threading.Thread(target=self.pusher.loop, args=(self.stop,), name="push", daemon=True).start()

    # ---------- 业务 ----------
    def project(self, target):
        home = read_json(HOME_JSON, {}) or {}
        return next((p for p in home.get("projects", []) if p.get("id") == target), None)

    def post_edit(self, body):
        ts = now_iso()
        e = ledger.normalize_edit(body, ts)
        err = apply_edits.validate(e)
        if err:
            raise ValueError(err)
        with self.svc:
            with data_lock():
                cur = ledger.read_ledger(EDITS).get(e["id"])
                if not cur:
                    ledger.append(EDITS, e)
                    self._commit(["edits.jsonl"], f"edits: + {e['id']}")
            if cur and cur.get("state") != "pending":
                return 200, self._edit_resp(cur)
            lines = self.process_pending()
        if lines is None or e["id"] not in lines:
            return 202, {"edit": {"id": e["id"], "state": "pending"}}
        return 200, self._edit_resp({**e, **lines[e["id"]]})

    def _edit_resp(self, rec):
        edit = {k: rec[k] for k in ("id", "state", "summary", "error") if rec.get(k)}
        home = read_json(HOME_JSON, {}) or {}
        return {"edit": edit, "project": self.project(rec.get("target")), "generated": home.get("generated")}

    def post_reading(self, body):
        url = str((body or {}).get("url", "")).strip()
        if not re.match(r"^https?://", url):
            raise ValueError("url 必须是 http(s)")
        why = str(body.get("why", "")).strip()
        with data_lock():
            for r in ledger.read_ledger(READINGS).values():
                if r.get("url") == url:
                    return 200, r
        title, _ = fetch_title(url)                       # 10 s 超时，不持锁
        entry = add_link.build_entry(url, why, title)
        with self.svc:
            with data_lock():
                ledger.append(READINGS, entry)
                self._commit(["readings.jsonl"], f"readings: + {title[:60]}")
            self.rebuild()
        return 201, entry

    def patch_reading(self, key, body):
        status = (body or {}).get("status")
        if status not in ledger.READING_STATUS:
            raise ValueError(f"status 必须是 {sorted(ledger.READING_STATUS)}")
        with self.svc:
            with data_lock():
                rec = ledger.find_reading(ledger.read_ledger(READINGS), key)
                if not rec:
                    return 404, {"error": f"没有这条读物 {key}"}
                ledger.append(READINGS, {"id": rec["id"], "status": status, "ts": now_iso()})
                self._commit(["readings.jsonl"], f"readings: {rec['id']} → {status}")
            self.rebuild()
        return 200, {"id": rec["id"], "status": status}

    def notes_pending(self):
        with data_lock():
            return ledger.notes_pending(ledger.read_ledger(EDITS))

    def note_done(self, eid, body):
        with self.svc:
            with data_lock():
                rec = ledger.read_ledger(EDITS).get(eid)
                if not rec:
                    return 404, {"error": f"没有这条编辑 {eid}"}
                if rec.get("state") != "done":
                    line = {"id": eid, "state": "done", "ts": now_iso()}
                    if (body or {}).get("result"):
                        line["result"] = str(body["result"])
                    ledger.append(EDITS, line)
                    self._commit(["edits.jsonl"], f"edits: done {eid}")
            self.rebuild()
            was_done = rec.get("state") == "done"
            target = rec.get("target") or eid
            title = (self.project(target) or {}).get("title") or target
        if not was_done:
            self.fire(lambda: self.notifier.note_done(eid, title, target))
        return 200, {"id": eid, "state": "done"}

    def worktree_state(self):
        """(dirty, rebasing)；不取 data_lock，git 读不到时 dirty 为 None。"""
        gitdir = os.path.join(DATA_DIR, ".git")
        rebasing = any(os.path.isdir(os.path.join(gitdir, d)) for d in ("rebase-merge", "rebase-apply"))
        try:
            r = git("--no-optional-locks", "status", "--porcelain", timeout=10, check=False)
            dirty = bool(r.stdout.strip()) if r.returncode == 0 else None
        except Exception:  # noqa: BLE001
            dirty = None
        return dirty, rebasing

    def health(self):
        # 不取 data_lock：账本只追加，读到半行 parse_lines 会跳过，最多少计一条
        pending = len(ledger.pending_edits(ledger.read_ledger(EDITS)))
        pending_todos = len(todo_actions.summarize(todo_actions.read_records(TODO_ACTIONS), todo_actions.local_done())[2])
        try:
            import collect_dashboard as cd
            vault_ok = os.path.isdir(cd.VAULT_DASH)
        except Exception:  # noqa: BLE001
            vault_ok = False
        dirty, rebasing = self.worktree_state()
        ok = self.last_sync_ok is not False and self.last_push_ok is not False and dirty is False and not rebasing
        return {"ok": ok, "uptime_s": int(time.time() - self.started), "last_sync_down": self.last_sync_down,
                "last_sync_ok": self.last_sync_ok, "last_sync_error": self.last_sync_error,
                "last_push": self.last_push, "last_push_ok": self.last_push_ok, "last_push_error": self.last_push_error,
                "worktree_dirty": dirty, "rebasing": rebasing,
                "pending_edits": pending, "pending_todos": pending_todos, "vault_ok": vault_ok, "version": VERSION,
                "last_collect": self.sched.last_collect, "collect_failures": self.sched.collect_failures,
                "push_failures": self.pusher.failures, "apns": self.notifier.health()}

    def start_collect(self, body):
        srcs = (body or {}).get("sources")
        if srcs is not None and not isinstance(srcs, list):
            raise ValueError("sources 必须是数组")
        return 202, {"job": self.sched.start_job(srcs or None)["id"]}

    def get_collect(self, jid):
        job = self.sched.get_job(jid)
        return (200, job) if job else (404, {"error": f"没有这个 job {jid}"})


fetch_title = add_link.fetch_title   # 测试可替换，避免真联网


def file_response(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, None
    return raw, '"' + hashlib.sha256(raw).hexdigest()[:16] + '"'


def make_handler(svc):
    class H(BaseHTTPRequestHandler):
        server_version = "dibao/" + VERSION

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code, obj=None, raw=None, headers=None):
            body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if code != 304:
                self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body 过大")
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                raise ValueError("body 不是合法 JSON")

        def _dispatch(self, method):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ("127.0.0.1", "localhost", "[::1]"):
                return self._send(421, {"error": "只服务 127.0.0.1"})
            path = self.path.split("?", 1)[0]
            try:
                if method == "GET" and path == "/health":
                    return self._send(200, svc.health())
                if method == "GET" and path in ("/v1/home.json", "/v1/status.json"):
                    raw, etag = file_response(HOME_JSON if path == "/v1/home.json" else STATUS_JSON)
                    if raw is None:
                        return self._send(404, {"error": "还没有该文件"})
                    if self.headers.get("If-None-Match") == etag:
                        return self._send(304, raw=b"", headers={"ETag": etag})
                    return self._send(200, raw=raw, headers={"ETag": etag})
                if method == "GET" and path == "/v1/notes/pending":
                    return self._send(200, svc.notes_pending())
                if method == "POST" and path == "/v1/edits":
                    return self._send(*svc.post_edit(self._body()))
                if method == "POST" and path == "/v1/readings":
                    return self._send(*svc.post_reading(self._body()))
                if method == "PATCH" and (m := re.fullmatch(r"/v1/readings/([^/]+)", path)):
                    return self._send(*svc.patch_reading(m.group(1), self._body()))
                if method == "POST" and (m := re.fullmatch(r"/v1/notes/([^/]+)/done", path)):
                    return self._send(*svc.note_done(m.group(1), self._body()))
                if method == "POST" and (m := re.fullmatch(r"/v1/todos/([^/]+)/(complete|undo)", path)):
                    return self._send(*svc.post_todo(m.group(1), m.group(2), self._body()))
                if method == "POST" and path == "/v1/push/test":
                    return self._send(*svc.push_test(self._body()))
                if method == "POST" and path == "/v1/collect":
                    return self._send(*svc.start_collect(self._body()))
                if method == "GET" and (m := re.fullmatch(r"/v1/collect/([^/]+)", path)):
                    return self._send(*svc.get_collect(m.group(1)))
                return self._send(404, {"error": "没有这个路由"})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("处理 %s %s 出错", method, path)
                return self._send(500, {"error": str(e)[:200]})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

    return H


def setup_logging(cfg):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if cfg.log_file:
        os.makedirs(os.path.dirname(cfg.log_file), exist_ok=True)
        h = logging.handlers.RotatingFileHandler(cfg.log_file, maxBytes=1 << 20, backupCount=3, encoding="utf-8")
    else:
        h = logging.StreamHandler()
    h.setFormatter(fmt)
    log.addHandler(h)
    log.setLevel(logging.INFO)
    slog = logging.getLogger("dibao.sched")       # 调度日志单独成文件：同目录 sched.log，轮转同 api.log
    if cfg.log_file:
        sh = logging.handlers.RotatingFileHandler(os.path.join(os.path.dirname(cfg.log_file), "sched.log"),
                                                  maxBytes=1 << 20, backupCount=3, encoding="utf-8")
        sh.setFormatter(fmt)
        slog.addHandler(sh)
        slog.propagate = False
    slog.setLevel(logging.INFO)


def make_server(svc, port=None):
    return ThreadingHTTPServer(("127.0.0.1", port if port is not None else svc.cfg.port), make_handler(svc))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-push", action="store_true", help="不 push（演练 / 测试）")
    ap.add_argument("--once-sync", action="store_true", help="启动时先同步一轮再开始服务")
    ap.add_argument("--selftest", action="store_true", help="起服务、请求一次 /health、打印后退出")
    ap.add_argument("--collect-secs", type=int, help="覆盖采集间隔（秒，演练用）")
    a = ap.parse_args()
    cfg = config.load()
    if a.collect_secs:
        cfg.collect_secs = a.collect_secs
    setup_logging(cfg)
    svc = Service(cfg, push=not a.no_push)
    svc.prepare_repo()
    srv = make_server(svc)               # bind 失败直接抛，launchd 会按 ThrottleInterval 重启
    log.info("dibao %s 监听 127.0.0.1:%s data=%s push=%s", VERSION, cfg.port, DATA_DIR, svc.push)
    if a.once_sync:
        svc.sync_cycle()
    if a.selftest:
        import urllib.request
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/health").read().decode())
        srv.shutdown()
        return 0
    svc.start_threads()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
