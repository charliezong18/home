"""H7：worker 报 retry（vault 暂时性 OSError）时 dibao 不写状态行，edit 保持 pending；连续 EDIT_RETRY_MAX 次才记 failed。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402
import config  # noqa: E402
import dibao  # noqa: E402
import ledger  # noqa: E402

RETRY_STUB = os.path.join(_env.ROOT, "retry_worker.py")
STATE_FILE = os.path.join(_env.ROOT, "retry_worker.state")
with open(RETRY_STUB, "w") as f:
    f.write('''
import json, os, sys
edits = json.load(sys.stdin)
ok = os.path.exists(%r)                 # 状态文件存在 = Drive 恢复了
out = []
for e in edits:
    if e["target"] != "p:flaky":
        out.append({"id": e["id"], "state": "failed", "error": "不是本测试的", "changed": False})
    elif ok:
        out.append({"id": e["id"], "state": "applied", "summary": "ok", "changed": False})
    else:
        out.append({"id": e["id"], "state": "retry", "error": "[Errno 1] Operation not permitted", "changed": False})
json.dump(out, sys.stdout)
''' % STATE_FILE)


class EditRetryTest(unittest.TestCase):
    def setUp(self):
        cfg = config.load()
        cfg.worker_timeout = 10
        cfg.worker_cmd = [sys.executable, RETRY_STUB]
        self.svc = dibao.Service(cfg, push=False)
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    def state(self, eid):
        return ledger.read_ledger(dibao.EDITS)[eid]["state"]

    def test_transient_error_stays_pending_then_applies(self):
        code, body = self.svc.post_edit({"id": "e:retry1", "target": "p:flaky", "fields": {"next": "x"}})
        self.assertEqual((code, body["edit"]["state"]), (202, "pending"))
        self.assertEqual(self.state("e:retry1"), "pending")
        self.assertEqual(self.svc.process_pending(), {})              # 再来一轮仍然 pending，没写状态行
        self.assertEqual(self.state("e:retry1"), "pending")
        open(STATE_FILE, "w").close()                                   # Drive 恢复
        lines = self.svc.process_pending()
        self.assertEqual(lines["e:retry1"]["state"], "applied")
        self.assertEqual(self.state("e:retry1"), "applied")
        self.assertNotIn("e:retry1", self.svc.edit_retries)

    def test_gives_up_after_retry_cap(self):
        self.svc.post_edit({"id": "e:retry2", "target": "p:flaky", "fields": {"next": "y"}})   # 第 1 次
        for _ in range(dibao.EDIT_RETRY_MAX - 2):
            self.assertEqual(self.svc.process_pending(), {})
        self.assertEqual(self.state("e:retry2"), "pending")
        lines = self.svc.process_pending()                               # 第 EDIT_RETRY_MAX 次
        self.assertEqual(lines["e:retry2"]["state"], "failed")
        self.assertIn("重试", lines["e:retry2"]["error"])
        self.assertEqual(self.state("e:retry2"), "failed")
        self.assertEqual(_env.sh("git", "-C", _env.DATA, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
