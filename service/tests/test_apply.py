"""service/apply.py：worker / 重采子进程封装。用一次性的 python -c 子进程当 worker，不碰 vault、不联网。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402,F401
import apply  # noqa: E402

PY = sys.executable
ECHO = ("import json,sys; es=json.load(sys.stdin); "
        "json.dump([{'id':e['id'],'state':'applied','summary':e['fields']['next'],'changed':True} for e in es], sys.stdout, ensure_ascii=False)")


class RunWorkerTest(unittest.TestCase):
    def setUp(self):
        self.logs = []

    def run_worker(self, code, edits=None, timeout=10):
        return apply.run_worker([PY, "-c", code], edits if edits is not None else [{"id": "e:1", "fields": {"next": "下一步"}}],
                                timeout, self.logs.append)

    def test_success_roundtrips_stdin_stdout_utf8(self):
        out = self.run_worker(ECHO, [{"id": "e:1", "fields": {"next": "中文 ✓"}}, {"id": "e:2", "fields": {"next": "b"}}])
        self.assertEqual(out, [{"id": "e:1", "state": "applied", "summary": "中文 ✓", "changed": True},
                               {"id": "e:2", "state": "applied", "summary": "b", "changed": True}])
        self.assertEqual(self.logs, [])

    def test_same_input_same_output(self):                              # 幂等：worker 是纯函数式调用，无隐藏状态
        a, b = self.run_worker(ECHO), self.run_worker(ECHO)
        self.assertEqual(a, b)

    def test_timeout_returns_none_and_logs(self):
        self.assertIsNone(self.run_worker("import time; time.sleep(30)", timeout=0.5))
        self.assertIn("被杀", self.logs[0])
        self.assertIn("1 条留到下轮", self.logs[0])

    def test_nonzero_exit_returns_none(self):
        self.assertIsNone(self.run_worker("import sys; sys.stderr.write('boom'); sys.exit(3)"))
        self.assertIn("rc=3", self.logs[0])
        self.assertIn("boom", self.logs[0])

    def test_non_json_output_returns_none(self):
        self.assertIsNone(self.run_worker("print('not json')"))
        self.assertIn("不是 JSON", self.logs[0])

    def test_non_list_json_returns_none(self):
        self.assertIsNone(self.run_worker("print('{\"id\": 1}')"))

    def test_failed_state_passes_through(self):
        code = "import json,sys; json.load(sys.stdin); print(json.dumps([{'id':'e:1','state':'failed','error':'找不到','changed':False}]))"
        self.assertEqual(self.run_worker(code)[0]["state"], "failed")

    def test_default_worker_is_apply_edits(self):
        self.assertEqual(apply.DEFAULT_WORKER[1:], [os.path.join(apply.COLLECTORS, "apply_edits.py"), "--worker"])
        self.assertTrue(os.path.exists(apply.DEFAULT_WORKER[1]))
        self.assertTrue(os.path.exists(apply.DEFAULT_RECOLLECT[1]))


class RecollectTest(unittest.TestCase):
    def setUp(self):
        self.logs = []

    def test_ok(self):
        self.assertTrue(apply.recollect([PY, "-c", "pass"], 10, self.logs.append))
        self.assertEqual(self.logs, [])

    def test_failure_logged(self):
        self.assertFalse(apply.recollect([PY, "-c", "raise SystemExit(2)"], 10, self.logs.append))
        self.assertIn("rc=2", self.logs[0])

    def test_timeout_logged(self):
        self.assertFalse(apply.recollect([PY, "-c", "import time; time.sleep(30)"], 0.5, self.logs.append))
        self.assertIn("超过", self.logs[0])


if __name__ == "__main__":
    unittest.main()
