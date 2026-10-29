"""dibao 调度：采集轮（等价 run_all.sh）、collect job 表、push 防抖（#187 A 期·WP-09b）。

采集器仍以子进程跑（超时 120 s），status.py 记成败逻辑不变；build 步骤走本服务的 rebuild()
（build_home 的 sync_down/commit_and_push 已被 Service 换成本地提交，push 交给 Pusher 防抖）。
"""
import logging
import os
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTORS = os.path.join(os.path.dirname(HERE), "collectors")
SOURCES = {"menxia": "collect_menxia.py", "dashboard": "collect_dashboard.py",
           "countdown": "collect_countdown.py", "backlog": "collect_backlog.py"}  # keep in sync with collectors/run_all.sh
COLLECT_TIMEOUT = 120
MAX_JOBS = 50
PUSH_ALERT_AFTER = 3
log = logging.getLogger("dibao.sched")


def notify(text, today):
    """走 collectors/notify.send（含 AI_HOME_NTFY_DRYRUN 开关）；失败只记日志。"""
    import notify as ntfy
    return ntfy.send(text, title="AI home dibao")


class Pusher:
    """本地提交后 debounce 秒内 push 一次；连续失败 PUSH_ALERT_AFTER 次推 ntfy（每天 ≤1 条）。clock 可注入。"""

    def __init__(self, push_fn, debounce, clock=time.time, alert=notify, today=None):
        self.push_fn, self.debounce, self.clock, self.alert = push_fn, debounce, clock, alert
        self.today = today or (lambda: time.strftime("%Y-%m-%d"))
        self.first_dirty = None
        self.failures = 0
        self.last_alert_date = None
        self.lock = threading.Lock()

    def mark(self):
        with self.lock:
            if self.first_dirty is None:
                self.first_dirty = self.clock()

    def tick(self):
        """到期就 push；返回 True=本次 push 了且成功，False=失败，None=没到期。"""
        with self.lock:
            if self.first_dirty is None or self.clock() - self.first_dirty < self.debounce:
                return None
            self.first_dirty = None
        try:
            ok = bool(self.push_fn())
        except Exception:  # noqa: BLE001
            log.exception("push 异常")
            ok = False
        if ok:
            self.failures = 0
            return True
        self.failures += 1
        self.mark()                               # 失败：下个防抖窗口重试
        if self.failures >= PUSH_ALERT_AFTER and self.last_alert_date != self.today():
            if self.alert(f"AI 主页：dibao push 连续失败 {self.failures} 次", self.today()):
                self.last_alert_date = self.today()
        return False

    def loop(self, stop):
        while not stop.wait(1):
            self.tick()


class Scheduler:
    def __init__(self, svc, runner=subprocess.run):
        self.svc, self.runner = svc, runner
        self.round_lock = threading.Lock()         # 同一时刻只跑一轮
        self.jobs, self.order = {}, []
        self.jobs_lock = threading.Lock()
        self.last_collect = None
        self.collect_failures = 0                  # 连续「有源失败」的轮数

    # ---------- 单源 / 一轮 ----------
    def _status(self, source, result, msg=""):
        cmd = [sys.executable, os.path.join(COLLECTORS, "status.py"), source, result]
        if msg:
            cmd.append(msg)
        try:
            self.runner(cmd, capture_output=True, text=True, timeout=30, cwd=COLLECTORS)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("status.py %s %s 失败：%s", source, result, e)

    def run_source(self, name):
        script = os.path.join(COLLECTORS, SOURCES[name])
        try:
            r = self.runner([sys.executable, script], capture_output=True, text=True,
                            timeout=COLLECT_TIMEOUT, cwd=COLLECTORS)
        except subprocess.TimeoutExpired:
            self._status(name, "fail", f"超时 {COLLECT_TIMEOUT}s")
            return {"ok": False, "error": f"超时 {COLLECT_TIMEOUT}s"}
        except OSError as e:
            self._status(name, "fail", str(e))
            return {"ok": False, "error": str(e)}
        out = (r.stdout or "") + (r.stderr or "")
        last = out.strip().splitlines()[-1] if out.strip() else ""
        if r.returncode == 0:
            self._status(name, "ok")
            return {"ok": True}
        self._status(name, "fail", last)
        return {"ok": False, "error": last[:300]}

    def run_round(self, sources=None):
        names = [s for s in SOURCES if sources is None or s in sources]
        with self.round_lock:
            res = {n: self.run_source(n) for n in names}
            try:
                with self.svc.svc:
                    rc = self.svc.rebuild()          # build_home.main：0 ok / 3 readings 解析失败
                if rc == 3:
                    self._status("readings", "fail", "readings.jsonl 解析失败")
                else:
                    self._status("readings", "ok")
                self._status("build", "ok")
                res["build"] = {"ok": True}
            except Exception as e:  # noqa: BLE001
                log.exception("build 失败")
                self._status("build", "fail", str(e)[:200])
                res["build"] = {"ok": False, "error": str(e)[:200]}
            from common import now_iso
            self.last_collect = now_iso()
            failed = [k for k, v in res.items() if not v["ok"]]
            self.collect_failures = self.collect_failures + 1 if failed else 0
            log.info("collect 轮结束 sources=%s failed=%s", names, failed)
        return res

    # ---------- job ----------
    def start_job(self, sources=None):
        unknown = [s for s in (sources or []) if s not in SOURCES]
        if unknown:
            raise ValueError(f"未知源 {unknown}，可选 {sorted(SOURCES)}")
        jid = "c-" + uuid.uuid4().hex[:8]
        job = {"id": jid, "state": "running", "sources": sources or list(SOURCES)}
        with self.jobs_lock:
            self.jobs[jid] = job
            self.order.append(jid)
            while len(self.order) > MAX_JOBS:
                self.jobs.pop(self.order.pop(0), None)

        def work():
            try:
                job["result"] = self.run_round(sources)
                job["state"] = "done"
            except Exception as e:  # noqa: BLE001
                log.exception("collect job 异常")
                job.update(state="error", error=str(e)[:200])

        threading.Thread(target=work, name=jid, daemon=True).start()
        return job

    def get_job(self, jid):
        with self.jobs_lock:
            return self.jobs.get(jid)

    def loop(self, stop, secs):
        while not stop.wait(secs):
            try:
                self.run_round()
            except Exception:  # noqa: BLE001
                log.exception("定时采集异常")
