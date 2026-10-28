import os
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402,F401
import config  # noqa: E402
import dibao  # noqa: E402
import dibao_sched as sched  # noqa: E402  (renamed: avoid shadowing stdlib sched)
from test_http import req  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class PusherTest(unittest.TestCase):
    def test_debounce_two_changes_one_push(self):
        clock, pushes = FakeClock(), []
        p = sched.Pusher(lambda: pushes.append(1) or True, 60, clock=clock)
        p.mark()
        clock.t += 30
        p.mark()                                  # 窗口内第二次变更不重置计时
        self.assertIsNone(p.tick())
        clock.t += 31
        self.assertTrue(p.tick())
        self.assertIsNone(p.tick())               # 已推过，无新变更
        self.assertEqual(len(pushes), 1)

    def test_three_failures_alert_once_per_day(self):
        clock, alerts = FakeClock(), []
        p = sched.Pusher(lambda: False, 60, clock=clock, alert=lambda t, d: alerts.append(t) or True,
                         today=lambda: "2026-10-28")
        p.mark()
        for _ in range(5):
            clock.t += 61
            self.assertFalse(p.tick())
        self.assertEqual(p.failures, 5)
        self.assertEqual(len(alerts), 1)          # 第 3 次起告警，当天只一条


class SchedulerTest(unittest.TestCase):
    def test_collect_timeout_records_fail(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            if cmd[1].endswith("collect_menxia.py"):
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

        svc = dibao.Service(config.load(), push=False)
        s = sched.Scheduler(svc, runner=runner)
        out = s.run_source("menxia")
        self.assertFalse(out["ok"])
        self.assertIn("超时", out["error"])
        status_calls = [c for c in calls if c[1].endswith("status.py")]
        self.assertEqual(status_calls[0][2:4], ["menxia", "fail"])

    def test_collect_endpoint_returns_job_id(self):
        svc = dibao.Service(config.load(), push=False)
        svc.sched.runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "ok\n", "")
        srv = dibao.make_server(svc, 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            code, body, _ = req(port, "POST", "/v1/collect", {"sources": ["menxia"]})
            self.assertEqual(code, 202)
            jid = body["job"]
            for _ in range(100):
                code, job, _ = req(port, "GET", f"/v1/collect/{jid}")
                if job["state"] != "running":
                    break
                time.sleep(0.1)
            self.assertEqual((code, job["state"]), (200, "done"))
            self.assertTrue(job["result"]["menxia"]["ok"])
            self.assertEqual(req(port, "GET", "/v1/collect/c-none")[0], 404)
            _, h, _ = req(port, "GET", "/health")
            self.assertTrue(h["last_collect"])
            self.assertEqual((h["collect_failures"], h["push_failures"]), (0, 0))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
