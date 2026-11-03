"""2026-11 测试补课：dibao 端点与 todo-actions 队列的边角（全部用假 gog / 假采集器，不联网、不碰真实凭据）。

覆盖：/v1/todos/<id>/complete|undo（幂等键、隐私、202、already）、队列（同 id 去重、退避重试、断网、
批中异常、本机账本补写丢失的状态行）、/v1/collect（假采集器）、/v1/edits（坏 body）、/health（rebasing、pending_todos）。
所有任务 id / 标题都是虚构的。
"""
import http.client
import os
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402
import dibao  # noqa: E402
import todo_actions as ta  # noqa: E402
from test_http import req  # noqa: E402
from test_todo_actions import FakeGog, _svc, action, states, write_remote_lines  # noqa: E402

OFFLINE = "Get \"https://tasks.googleapis.com/...\": dial tcp: lookup tasks.googleapis.com: no such host"


def first_lines(aid):
    return [r for r in ta.read_records(dibao.TODO_ACTIONS) if r["id"] == aid and r.get("op")]


class Server:
    def __init__(self, svc):
        self.svc = svc
        self.srv = dibao.make_server(svc, 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class QueueTest(unittest.TestCase):
    def test_duplicate_pending_lines_execute_once(self):
        """网页重发 / 两端同时写：同 id 的 pending 首行出现两次，只执行一次、只写一条状态行。"""
        g = FakeGog()
        svc = _svc(g)
        a = action("QD1", aid="qdup-1")
        write_remote_lines(a, dict(a), dict(a))
        lines = svc.process_todos()
        self.assertEqual(lines["qdup-1"]["state"], "done")
        self.assertEqual(g.mutations(), [("done", "QD1")])
        terminal = [r for r in ta.read_records(dibao.TODO_ACTIONS) if r["id"] == "qdup-1" and not r.get("op")]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(svc.process_todos(), {})

    def test_offline_errors_are_retried(self):
        """断网时 gog 报 DNS 失败（no such host）：属于瞬时错误，应退避重试，而不是一次就判 failed。"""
        sleeps = []
        g = FakeGog(fail={"QO1"}, status={}, stderr=OFFLINE)          # get 也读不到（离线）
        state, err = ta.execute(action("QO1"), g, sleep=sleeps.append)
        self.assertEqual(state, "failed")
        self.assertEqual(sleeps, [2, 4, 8])
        self.assertIn("已重试 3 次", err)
        for msg in ("connect: network is unreachable", "dial tcp 1.2.3.4:443: connect: no route to host",
                    "read tcp: i/o timeout", "net/http: TLS handshake timeout", "connection reset by peer"):
            self.assertTrue(ta.transient(1, msg), msg)
        for msg in ("googleapi: Error 404: notFound", "googleapi: Error 400: Invalid task id", "invalid_grant"):
            self.assertFalse(ta.transient(1, msg), msg)

    def test_offline_then_recovered_replay(self):
        """离线一轮：git pull 失败 + gog 不通 → 本地队列仍被执行、失败原因落账本并本地提交；
        恢复后客户端换新 id 再勾一次 → done；旧的 failed 不被自动重放。"""
        g = FakeGog(fail={"QR1"}, status={}, stderr=OFFLINE)
        svc = _svc(g)
        write_remote_lines(action("QR1", aid="qrep-1"))
        orig = dibao.sync_down

        def offline():
            raise RuntimeError("fatal: unable to access 'https://github.com/x/y.git/': Could not resolve host")
        dibao.sync_down = offline
        try:
            svc.sync_cycle()
            h = svc.health()
            self.assertEqual((h["ok"], h["last_sync_ok"], h["pending_todos"]), (False, False, 0))
            self.assertEqual(states()["qrep-1"]["state"], "failed")
            self.assertIn("no such host", states()["qrep-1"]["error"])
            self.assertEqual(_env.sh("git", "-C", _env.DATA, "status", "--porcelain"), "")
            n = len(g.mutations())
            g.fail.clear()                                             # 网络恢复
            dibao.sync_down = lambda: None
            svc.sync_cycle()
            self.assertEqual(len(g.mutations()), n)                    # failed 粘住，不自动重放
            self.assertTrue(svc.health()["last_sync_ok"])
            write_remote_lines(action("QR1", aid="qrep-2"))            # 用户再勾一次（新 id）
            svc.sync_cycle()
        finally:
            dibao.sync_down = orig
        self.assertEqual(states()["qrep-2"]["state"], "done")
        self.assertEqual(g.status["QR1"], "completed")

    def test_remembered_but_unrecorded_line_is_replayed(self):
        """执行后已记本机账本、但状态行没进 home-data（进程在提交前挂了 / 提交丢失）：
        下一轮不能重放 gog，但要把记住的状态行补进 todo-actions.jsonl，否则客户端永远看到 pending。"""
        g = FakeGog()
        svc = _svc(g)
        write_remote_lines(action("QL1", aid="qlost-1"), action("QL2", "undo", aid="qlost-2"))
        ta.remember(ta.state_line("qlost-1", "done", "ok", "2026-11-01T17:00:00Z"))
        ta.remember(ta.state_line("qlost-2", "failed", "gog undo rc=1: boom", "2026-11-01T17:00:01Z"))
        lines = svc.process_todos()
        self.assertEqual(g.mutations(), [])                            # 没有重放
        self.assertEqual({k: v["state"] for k, v in (lines or {}).items()},
                         {"qlost-1": "done", "qlost-2": "failed"})
        f = states()
        self.assertEqual((f["qlost-1"]["result"], f["qlost-2"]["error"]), ("ok", "gog undo rc=1: boom"))
        self.assertEqual(_env.sh("git", "-C", _env.DATA, "status", "--porcelain"), "")
        self.assertEqual(svc.process_todos(), {})                      # 补写一次就够
        self.assertEqual(svc.health()["pending_todos"], 0)

    def test_exception_mid_batch_keeps_other_state_lines(self):
        """某条执行时抛了意料外的异常（例如 gog 输出不是 UTF-8）：同批其他已执行的条目的状态行不能丢。"""
        class Boom(FakeGog):
            def __call__(self, cmd, **kw):
                if cmd[4] == "QX2":
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
                return super().__call__(cmd, **kw)
        g = Boom()
        svc = _svc(g)
        write_remote_lines(action("QX1", aid="qx-1"), action("QX2", aid="qx-2"), action("QX3", aid="qx-3"))
        svc.process_todos()
        f = states()
        self.assertEqual({k: f[k]["state"] for k in ("qx-1", "qx-2", "qx-3")},
                         {"qx-1": "done", "qx-2": "failed", "qx-3": "done"})
        self.assertIn("UnicodeDecodeError", f["qx-2"]["error"])
        self.assertEqual(g.mutations(), [("done", "QX1"), ("done", "QX3")])

    def test_non_utf8_output_after_gog_ran_is_done(self):
        """真实场景：gog 已经执行（Google Tasks 里已勾掉），之后解码 stdout/stderr 时才遇到非 UTF-8。
        假 gog 照 subprocess.run(text=True) 的行为：没传 errors="replace" 就在变更之后抛 UnicodeDecodeError。
        不能被标成粘住的 failed。"""
        class BadBytes(FakeGog):
            def __call__(self, cmd, **kw):
                r = super().__call__(cmd, **kw)                        # 先真的执行（变更已发生）
                if cmd[4] != "QU1":
                    return r
                raw = b"\xff\xfe ok"
                if kw.get("text") and kw.get("errors") != "replace":
                    raise UnicodeDecodeError("utf-8", raw, 0, 1, "invalid start byte")
                return subprocess.CompletedProcess(cmd, r.returncode, raw.decode("utf-8", "replace"),
                                                   raw.decode("utf-8", "replace"))
        g = BadBytes()
        svc = _svc(g)
        write_remote_lines(action("QU1", aid="qu-1"))
        svc.process_todos()
        f = states()
        self.assertEqual(f["qu-1"]["state"], "done")
        self.assertEqual(g.mutations(), [("done", "QU1")])
        self.assertEqual(g.status["QU1"], "completed")

    def test_pending_counted_in_health(self):
        svc = _svc(FakeGog())
        base = svc.health()["pending_todos"]
        write_remote_lines(action("QH1", aid="qh-1"), action("QH2", "delete", aid="qh-2"))
        self.assertEqual(svc.health()["pending_todos"], base + 2)      # 不合法的也算待处理（会被标 failed）
        svc.process_todos()
        self.assertEqual(svc.health()["pending_todos"], base)
        self.assertEqual(states()["qh-2"]["state"], "failed")


class HttpTodoPassTest(unittest.TestCase):
    def setUp(self):
        self.gog = FakeGog(fail={"HA1"}, status={"HA1": "completed"})
        self.s = Server(_svc(self.gog))
        self.addCleanup(self.s.close)

    def post(self, path, body=None, **kw):
        return req(self.s.port, "POST", path, body, **kw)

    def test_response_has_no_title_and_already_is_200(self):
        code, body, _ = self.post("/v1/todos/HA1/complete", {"id": "ha-1", "title": "虚构的私密标题"})
        self.assertEqual((code, body["action"]["state"], body["action"]["result"]), (200, "done", "already"))
        self.assertEqual(set(body["action"]), {"id", "task_id", "op", "state", "result"})
        self.assertEqual(first_lines("ha-1")[0]["title"], "虚构的私密标题")   # 账本里有（日志用），响应里没有

    def test_same_id_while_batch_running_appends_once(self):
        """另一批正在跑（todo_lock 被占）：同 id 连发两次都 202，账本只有一条首行；锁放开后只执行一次。"""
        svc = self.s.svc
        self.assertTrue(svc.todo_lock.acquire(timeout=5))
        try:
            for _ in range(2):
                code, body, _ = self.post("/v1/todos/HB1/undo", {"id": "hb-1"})
                self.assertEqual((code, body["action"]["state"]), (202, "pending"))
            self.assertEqual(len(first_lines("hb-1")), 1)
            self.assertEqual(svc.health()["pending_todos"] >= 1, True)
        finally:
            svc.todo_lock.release()
        code, body, _ = self.post("/v1/todos/HB1/undo", {"id": "hb-1"})   # 客户端轮询：同 id 再发 = 执行 / 取结果
        self.assertEqual((code, body["action"]["state"]), (200, "done"))
        self.assertEqual(self.gog.mutations(), [("undo", "HB1")])
        self.assertEqual(self.post("/v1/todos/HB1/undo", {"id": "hb-1"})[0], 200)
        self.assertEqual(self.gog.mutations(), [("undo", "HB1")])

    def test_offline_gog_502_with_reason(self):
        self.gog.fail.add("HC1")
        self.gog.stderr = OFFLINE
        code, body, _ = self.post("/v1/todos/HC1/complete", {"id": "hc-1"})
        self.assertEqual((code, body["action"]["state"]), (502, "failed"))
        self.assertIn("no such host", body["action"]["error"])
        self.assertEqual(len(self.gog.mutations()), 4)                  # 1 + 3 次重试

    def test_bad_bodies(self):
        self.assertEqual(self.post("/v1/todos/HD1/complete", {"from": "ios"})[0], 400)
        self.assertEqual(self.post("/v1/todos/HD1/complete", {"id": 123})[0], 200)   # 数字 id 按字符串收
        c = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=10)
        c.request("POST", "/v1/todos/HD2/complete", body=b"{not json", headers={"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 400)
        c.close()
        self.assertEqual(first_lines("HD2"), [])


class CollectEndpointTest(unittest.TestCase):
    def setUp(self):
        self.svc = _svc(FakeGog())
        self.calls = []

        def runner(cmd, **kw):
            script = os.path.basename(cmd[1])
            self.calls.append([script, *cmd[2:]])
            if script == "collect_gtasks.py":
                return subprocess.CompletedProcess(cmd, 1, "", "gtasks FAIL: gog rc=1: auth expired")
            return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
        self.svc.sched.runner = runner
        self.s = Server(self.svc)
        self.addCleanup(self.s.close)

    def wait_job(self, jid):
        for _ in range(200):
            code, job, _ = req(self.s.port, "GET", f"/v1/collect/{jid}")
            if job["state"] != "running":
                return code, job
            time.sleep(0.05)
        self.fail("collect job 没结束")

    def test_subset_job_runs_fake_collectors(self):
        code, body, _ = req(self.s.port, "POST", "/v1/collect", {"sources": ["countdown", "gtasks"]})
        self.assertEqual(code, 202)
        code, job = self.wait_job(body["job"])
        self.assertEqual((code, job["state"], job["sources"]), (200, "done", ["countdown", "gtasks"]))
        self.assertEqual(job["result"]["countdown"], {"ok": True})
        self.assertEqual(job["result"]["gtasks"]["ok"], False)
        self.assertIn("auth expired", job["result"]["gtasks"]["error"])
        self.assertTrue(job["result"]["build"]["ok"])
        ran = [c[0] for c in self.calls if c[0] != "status.py"]
        self.assertEqual(ran, ["collect_countdown.py", "collect_gtasks.py"])   # 只跑点名的源
        self.assertIn(["status.py", "gtasks", "fail", "gtasks FAIL: gog rc=1: auth expired"], self.calls)
        self.assertIn(["status.py", "countdown", "ok"], self.calls)
        h = req(self.s.port, "GET", "/health")[1]
        self.assertEqual(h["collect_failures"], 1)
        self.assertIsNotNone(h["last_collect"])

    def test_validation(self):
        self.assertEqual(req(self.s.port, "POST", "/v1/collect", {"sources": "gtasks"})[0], 400)
        self.assertEqual(req(self.s.port, "POST", "/v1/collect", {"sources": ["gtasks", "nope"]})[0], 400)
        self.assertEqual(req(self.s.port, "GET", "/v1/collect/c-nope")[0], 404)
        self.assertEqual(self.calls, [])


class EditsAndHealthTest(unittest.TestCase):
    def setUp(self):
        self.s = Server(_svc(FakeGog()))
        self.addCleanup(self.s.close)

    def raw(self, path, body, length=None):
        c = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=10)
        c.putrequest("POST", path)
        c.putheader("Content-Length", str(length if length is not None else len(body)))
        c.endheaders()
        if body:
            c.send(body)
        r = c.getresponse()
        out = (r.status, r.read())
        c.close()
        return out

    def test_edits_bad_bodies(self):
        code, raw = self.raw("/v1/edits", b"", length=dibao.MAX_BODY + 1)
        self.assertEqual(code, 400)
        self.assertIn("过大".encode(), raw)
        self.assertEqual(self.raw("/v1/edits", b"[1,2")[0], 400)
        self.assertEqual(req(self.s.port, "POST", "/v1/edits", {"target": "p:test", "fields": {"next": "x"},
                                                                 "from": "ios"})[0], 400)

    def test_health_reports_rebasing(self):
        d = os.path.join(_env.DATA, ".git", "rebase-merge")
        os.makedirs(d)
        try:
            h = req(self.s.port, "GET", "/health")[1]
            self.assertEqual((h["ok"], h["rebasing"]), (False, True))
        finally:
            os.rmdir(d)
        h = req(self.s.port, "GET", "/health")[1]
        self.assertEqual(h["rebasing"], False)
        for k in ("pending_todos", "pending_edits", "apns", "push_failures", "worktree_dirty", "version"):
            self.assertIn(k, h)


class DigestDstTest(unittest.TestCase):
    """早晚报的「今天勾完」按洛杉矶日期算；DST 结束（2026-11-01）后午夜变成 08:00Z。"""

    def test_done_today_and_slots_across_dst(self):
        import datetime as dt
        import digest
        U = dt.timezone.utc
        recs = [{"id": "d1", "op": "complete", "task_id": "T1", "state": "pending", "ts": "2026-11-02T07:20:00Z"},
                {"id": "d1", "state": "done", "ts": "2026-11-02T07:30:00Z", "result": "ok"},   # 11-01 23:30 PST
                {"id": "d2", "op": "complete", "task_id": "T2", "state": "pending", "ts": "2026-11-02T08:00:00Z"},
                {"id": "d2", "state": "done", "ts": "2026-11-02T08:00:01Z", "result": "ok"}]   # 11-02 00:00 PST
        self.assertEqual(digest.todos_done_today(recs, "2026-11-01"), 1)
        self.assertEqual(digest.todos_done_today(recs, "2026-11-02"), 1)
        self.assertEqual(digest.slot_day("morning", dt.datetime(2026, 10, 31, 15, 0, tzinfo=U)), "2026-10-31")  # 08:00 PDT
        self.assertIsNone(digest.slot_day("morning", dt.datetime(2026, 11, 1, 15, 0, tzinfo=U)))              # 07:00 PST（02:00 已切）
        self.assertEqual(digest.slot_day("morning", dt.datetime(2026, 11, 1, 16, 0, tzinfo=U)), "2026-11-01")   # 08:00 PST
        self.assertEqual(digest.slot_day("morning", dt.datetime(2026, 11, 2, 16, 29, tzinfo=U)), "2026-11-02")  # 补发窗口内
        self.assertIsNone(digest.slot_day("morning", dt.datetime(2026, 11, 2, 16, 30, tzinfo=U)))             # 窗口外
        self.assertEqual(digest.slot_day("evening", dt.datetime(2026, 11, 2, 5, 30, tzinfo=U)), "2026-11-01")  # 21:30 PST


if __name__ == "__main__":
    unittest.main()
