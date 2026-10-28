import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

import life_ingest as li
from build_home import due_items

TODAY = dt.date(2026, 10, 27)


def entry(**kw):
    base = {"id": "d:cal-test0001", "title": "示例会议", "date": "2026-11-01", "note": "", "source": "calendar"}
    return {**base, **kw}


class LifeIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(li, "DATA_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def run_cli(self, *argv):
        return li.main(list(argv))

    def rows(self):
        with open(li.ledger_path(), encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def test_validate_ok_and_defaults(self):
        v = li.validate(entry())
        self.assertEqual(v["status"], "open")

    def test_rejects_money_and_email(self):
        for bad in (entry(title="账单 $120"), entry(note="共 ¥300"), entry(note="from a@b.com"),
                    entry(title="x" * 61), entry(id="bad"), entry(date="2026-13-01"), entry(source="web")):
            with self.assertRaises(ValueError, msg=str(bad)):
                li.validate(bad)

    def test_add_dedup_and_update(self):
        self.run_cli("add", "--json", json.dumps(entry()), "--no-git")
        self.run_cli("add", "--json", json.dumps([entry(), entry(id="d:cal-test0002")]), "--no-git")
        self.assertEqual(len(self.rows()), 2)  # 同 id 同内容不重复写
        self.run_cli("add", "--json", json.dumps(entry(date="2026-11-02")), "--no-git")
        self.assertEqual(li.merged()["d:cal-test0001"]["date"], "2026-11-02")
        self.assertEqual(self.rows()[0]["from"], "claude-cron")

    def test_bad_batch_writes_nothing(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("add", "--json", json.dumps([entry(), entry(id="d:bill-x", title="$5 due", source="gmail")]),
                         "--no-git")
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(os.path.exists(li.ledger_path()))

    def test_done_hides_from_due(self):
        self.run_cli("add", "--json", json.dumps(entry()), "--no-git")
        self.run_cli("done", "--id", "d:cal-test0001", "--no-git")
        items, updated = li.load_life()
        self.assertEqual(items, [])
        self.assertTrue(updated)
        with self.assertRaises(SystemExit):
            self.run_cli("done", "--id", "d:cal-nope", "--no-git")

    def test_due_items_includes_life(self):
        self.run_cli("add", "--json", json.dumps(entry()), "--no-git")
        items, _ = li.load_life()
        due = due_items({}, items, TODAY)
        self.assertEqual([(d["id"], d["source"]) for d in due], [("d:cal-test0001", "calendar")])


class CliSubprocessTest(unittest.TestCase):
    """真起进程跑验收命令（含 AI_HOME_DATA 环境变量与退出码）。"""

    def run_script(self, data, payload):
        import subprocess
        import sys
        return subprocess.run([sys.executable, li.__file__, "add", "--json", json.dumps(payload), "--no-git"],
                              capture_output=True, text=True, env={**os.environ, "AI_HOME_DATA": data})

    def test_acceptance_commands(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_script(d, entry(title="测试"))
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "life: +1 skipped=0"))
            r = self.run_script(d, {"id": "d:bill-x", "title": "账单 $120", "date": "2026-11-01", "source": "gmail"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("金额", r.stderr)


if __name__ == "__main__":
    unittest.main()
