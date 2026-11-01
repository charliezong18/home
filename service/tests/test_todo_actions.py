"""Google Tasks 勾完成（todo-actions.jsonl）：全部用假 gog，绝不碰真实 Google Tasks。"""
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402
import cli  # noqa: E402
import config  # noqa: E402
import dibao  # noqa: E402
import todo_actions as ta  # noqa: E402
from test_http import req  # noqa: E402


class FakeGog:
    """假 gog：记录每次调用；done/undo 按 fail 集合决定 rc；get 返回 status 表里的状态。"""

    def __init__(self, fail=(), status=None, timeout=()):
        self.calls, self.fail, self.status, self.timeout = [], set(fail), dict(status or {}), set(timeout)

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        assert cmd[0] == ta.GOG and cmd[1] == "tasks", cmd
        sub, tid = cmd[2], cmd[4]
        assert sub in ("done", "undo", "get"), f"假 gog 收到不允许的子命令 {sub}"
        if tid in self.timeout:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        if sub == "get":
            st = self.status.get(tid)
            if st is None:
                return subprocess.CompletedProcess(cmd, 1, "", "not found")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"task": {"id": tid, "status": st}}), "")
        if tid in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "googleapi: Error 503: backend error")
        self.status[tid] = "completed" if sub == "done" else "needsAction"
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def mutations(self):
        return [(c[2], c[4]) for c in self.calls if c[2] in ("done", "undo")]


def action(task_id, op="complete", aid=None, frm="web"):
    return {"id": aid or str(uuid.uuid4()), "ts": "2026-10-31T18:00:00Z", "task_id": task_id, "op": op,
            "title": "示例", "from": frm, "state": "pending"}


class PureTest(unittest.TestCase):
    def test_only_two_ops(self):
        self.assertIsNone(ta.validate(action("T1", "complete")))
        self.assertIsNone(ta.validate(action("T1", "undo")))
        for op in ("delete", "create", "update", "rename", "clear", None, "done"):
            self.assertIn("不被允许", ta.validate(action("T1", op)), op)

    def test_task_id_shape(self):
        for bad in ("", "-rf", "a b", "a/b", "x" * 201, None, 123):
            self.assertIn("task_id", ta.validate(action(bad)), bad)
        self.assertIsNone(ta.validate(action("MTIzNDU2Nzg5_abc-DEF")))

    def test_parse_and_sticky_terminal(self):
        a, b = action("T1", aid="a"), action("T2", aid="b")
        recs = [a, b, {"id": "a", "state": "done", "ts": "t", "result": "ok"},
                dict(a)]                                              # 客户端重发同 id 的 pending 行
        first, final, todo = ta.summarize(recs)
        self.assertEqual(final["a"]["state"], "done")
        self.assertEqual([x["id"] for x in todo], ["b"])
        self.assertEqual(ta.summarize(recs, done_ids={"b"})[2], [])   # 本机账本记过 = 已处理
        text = "\n".join(json.dumps(r) for r in recs) + "\n{坏行\n[]\n"
        self.assertEqual(len(ta.ledger.parse_lines(text)), 4)

    def test_execute_calls_exact_gog(self):
        g = FakeGog()
        self.assertEqual(ta.execute(action("T1"), g, "@default"), ("done", "ok"))
        self.assertEqual(ta.execute(action("T1", "undo"), g, "@default"), ("done", "ok"))
        self.assertEqual(g.calls[0], ["gog", "tasks", "done", "@default", "T1", "--no-input", "--color=never"])
        self.assertEqual(g.calls[1][2], "undo")

    def test_already_in_target_state_is_success(self):
        g = FakeGog(fail={"T1"}, status={"T1": "completed"})
        self.assertEqual(ta.execute(action("T1"), g), ("done", "already"))
        g = FakeGog(fail={"T2"}, status={"T2": "needsAction"})
        self.assertEqual(ta.execute(action("T2", "undo"), g), ("done", "already"))

    def test_gog_failure_clear_error_no_retry(self):
        g = FakeGog(fail={"T1"}, status={"T1": "needsAction"})
        state, err = ta.execute(action("T1"), g)
        self.assertEqual(state, "failed")
        self.assertIn("gog done rc=1", err)
        self.assertIn("503", err)
        self.assertEqual([c[2] for c in g.calls], ["done", "get"])     # 一次执行 + 一次只读核对，不重试
        state, err = ta.execute(action("T9"), FakeGog(timeout={"T9"}))
        self.assertEqual(state, "failed")
        self.assertIn("超时", err)

    def test_invalid_op_never_reaches_gog(self):
        g = FakeGog()
        self.assertEqual(ta.execute(action("T1", "delete"), g)[0], "failed")
        self.assertEqual(g.calls, [])


def _svc(gog):
    cfg = config.load()
    cfg.worker_timeout = 3
    svc = dibao.Service(cfg, push=False)
    svc.prepare_repo()
    svc.todo_runner = gog
    svc.recollects = 0

    def recollect():
        svc.recollects += 1
    svc.recollect_gtasks = recollect
    return svc


def write_remote_lines(*recs):
    """模拟网页 / iOS 写入后被 pull 下来：直接追加并提交。"""
    for r in recs:
        ta.ledger.append(dibao.TODO_ACTIONS, r)
    _env.sh("git", "-C", _env.DATA, "add", "todo-actions.jsonl")
    _env.sh("git", "-C", _env.DATA, "commit", "-q", "-m", "web: todo actions")


def states():
    return ta.summarize(ta.read_records(dibao.TODO_ACTIONS))[1]


class ServiceTest(unittest.TestCase):
    def test_process_mixed_batch_idempotent(self):
        g = FakeGog(fail={"T4"}, status={"T4": "needsAction"})
        svc = _svc(g)
        a1, a2, a3, a4 = (action("T1", aid="s1"), action("T2", "undo", aid="s2"),
                          action("T3", "delete", aid="s3"), action("T4", aid="s4"))
        write_remote_lines(a1, a2, a3, a4)
        lines = svc.process_todos()
        self.assertEqual({k: v["state"] for k, v in lines.items()},
                         {"s1": "done", "s2": "done", "s3": "failed", "s4": "failed"})
        self.assertIn("不被允许", lines["s3"]["error"])
        self.assertIn("rc=1", lines["s4"]["error"])
        self.assertEqual(g.mutations(), [("done", "T1"), ("undo", "T2"), ("done", "T4")])
        self.assertEqual(svc.recollects, 1)                          # 有成功的 → 立刻重采一次
        f = states()
        self.assertEqual((f["s1"]["state"], f["s4"]["state"]), ("done", "failed"))
        self.assertEqual(_env.sh("git", "-C", _env.DATA, "status", "--porcelain"), "")
        self.assertIn("todos: apply 4", _env.sh("git", "-C", _env.DATA, "log", "--format=%s"))
        # 再跑 / 客户端重发同 id → 不重放；失败的也不自动重试
        write_remote_lines(dict(a1), dict(a4))
        self.assertEqual(svc.process_todos(), {})
        self.assertEqual(len(g.mutations()), 3)
        self.assertEqual(svc.health()["pending_todos"], 0)

    def test_sync_cycle_consumes_remote_lines(self):
        g = FakeGog()
        svc = _svc(g)
        orig = dibao.sync_down
        dibao.sync_down = lambda: None
        try:
            write_remote_lines(action("T5", aid="sync1"))
            self.assertEqual(svc.health()["pending_todos"], 1)
            svc.sync_cycle()
        finally:
            dibao.sync_down = orig
        self.assertEqual(states()["sync1"]["state"], "done")
        self.assertEqual(g.mutations(), [("done", "T5")])

    def test_complete_then_undo_in_order(self):
        g = FakeGog()
        svc = _svc(g)
        write_remote_lines(action("T6", aid="o1"), action("T6", "undo", aid="o2"))
        svc.process_todos()
        self.assertEqual(g.mutations(), [("done", "T6"), ("undo", "T6")])
        self.assertEqual(g.status["T6"], "needsAction")


class HttpTodoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gog = FakeGog(fail={"Tbad"}, status={"Tbad": "needsAction"})
        cls.svc = _svc(cls.gog)
        cls.srv = dibao.make_server(cls.svc, 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_complete_undo_and_idempotent(self):
        code, body, _ = req(self.port, "POST", "/v1/todos/H1/complete", {"id": "h-1", "title": "交周报"})
        self.assertEqual((code, body["action"]["state"], body["action"]["op"]), (200, "done", "complete"))
        n = len(self.gog.mutations())
        code, body, _ = req(self.port, "POST", "/v1/todos/H1/complete", {"id": "h-1"})
        self.assertEqual((code, body["action"]["state"]), (200, "done"))
        self.assertEqual(len(self.gog.mutations()), n)               # 同 id 不再执行
        self.assertEqual(req(self.port, "POST", "/v1/todos/H1/undo", {"id": "h-1"})[0], 400)   # 同 id 换操作
        code, body, _ = req(self.port, "POST", "/v1/todos/H1/undo", {})
        self.assertEqual((code, body["action"]["state"]), (200, "done"))
        self.assertEqual(self.gog.status["H1"], "needsAction")

    def test_gog_failure_502(self):
        code, body, _ = req(self.port, "POST", "/v1/todos/Tbad/complete", {})
        self.assertEqual((code, body["action"]["state"]), (502, "failed"))
        self.assertIn("rc=1", body["action"]["error"])

    def test_rejects(self):
        n = len(self.gog.calls)
        for path in ("/v1/todos/H2/delete", "/v1/todos/H2/update", "/v1/todos/H2"):
            self.assertEqual(req(self.port, "POST", path, {})[0], 404, path)
        self.assertEqual(req(self.port, "POST", "/v1/todos/-rf/complete", {})[0], 400)
        self.assertEqual(req(self.port, "POST", "/v1/todos/H2/complete", {"from": "web"})[0], 400)
        code, _, _ = req(self.port, "POST", "/v1/todos/H2/complete", {}, headers={"Host": "evil.example.com"})
        self.assertEqual(code, 421)                                   # 鉴权同 /v1/edits：只服务 127.0.0.1
        self.assertEqual(len(self.gog.calls), n)

    def test_cli(self):
        for argv, want in ((["todo-done", "C1", "--title", "示例"], ("done", "completed")),
                           (["todo-undo", "C1"], ("done", "needsAction"))):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["--port", str(self.port), *argv])
            out = json.loads(buf.getvalue()[buf.getvalue().index("{"):])
            self.assertEqual((rc, out["action"]["state"], self.gog.status["C1"]), (0, *want))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["--port", str(self.port), "todo-done", "Tbad"]), 1)
        self.assertIn("HTTP 502", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
