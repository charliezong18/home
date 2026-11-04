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

    def test_cron_readd_does_not_reopen_done(self):
        """H5：cron 每天重报同一事件（不带 status）不得把已 done 的条目改回 open；显式 status=open 仍可重开。"""
        self.run_cli("add", "--json", json.dumps(entry()), "--no-git")
        self.run_cli("done", "--id", "d:cal-test0001", "--no-git")
        n = len(self.rows())
        self.run_cli("add", "--json", json.dumps([entry(), entry(id="d:cal-test0002")]), "--no-git")
        self.assertEqual(li.merged()["d:cal-test0001"]["status"], "done")
        self.assertEqual(len(self.rows()), n + 1)                    # 只多了新条目那一行
        self.assertNotIn("d:cal-test0001", [d["id"] for d in li.load_life()[0]])
        self.run_cli("add", "--json", json.dumps(entry(note="改到 15:00")), "--no-git")   # 内容变了：更新但仍 done
        self.assertEqual((li.merged()["d:cal-test0001"]["note"], li.merged()["d:cal-test0001"]["status"]),
                         ("改到 15:00", "done"))
        self.run_cli("add", "--json", json.dumps(entry(note="改到 15:00", status="open")), "--no-git")
        self.assertEqual(li.merged()["d:cal-test0001"]["status"], "open")

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


class AutoIdTest(unittest.TestCase):
    def test_auto_id_stable(self):
        import life_ingest as L
        e = {"title": "示例：牙医复诊", "date": "2026-11-05", "source": "calendar"}
        a, b = L.validate(dict(e)), L.validate(dict(e))
        self.assertEqual(a["id"], b["id"])
        self.assertRegex(a["id"], r"^d:cal-[0-9a-f]{8}$")
        self.assertNotEqual(a["id"], L.validate({**e, "date": "2026-11-06"})["id"])

    def test_event_id_survives_title_change(self):
        """F-04 摄入侧：有 event_id 就按它出 id，改标题仍是同一条；event_id 不进账本。"""
        import life_ingest as L
        e = {"title": "示例：体检预约", "date": "2026-11-05", "source": "calendar", "event_id": "evt-demo-001"}
        a = L.validate(dict(e))
        b = L.validate({**e, "title": "⏰ 示例：体检预约（改）"})
        self.assertEqual(a["id"], b["id"])
        self.assertRegex(a["id"], r"^d:cal-[0-9a-f]{12}$")
        self.assertNotIn("event_id", a)
        self.assertNotEqual(a["id"], L.validate({**e, "event_id": "evt-demo-002"})["id"])


class EventIdCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(li, "DATA_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def test_event_id_flag_updates_same_row_and_private(self):
        e = {"title": "示例会议", "date": "2026-11-05", "note": "14:00", "source": "calendar"}
        li.main(["add", "--json", json.dumps(e), "--event-id", "evt-demo-9", "--no-git"])
        li.main(["add", "--json", json.dumps({**e, "title": "示例会议（改期说明）", "private": True}),
                 "--event-id", "evt-demo-9", "--no-git"])
        items, _ = li.load_life()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["title"], items[0].get("private")), ("示例会议（改期说明）", True))
        li.main(["add", "--json", json.dumps([{**e, "event_id": "evt-demo-9", "private": False}]), "--no-git"])
        self.assertNotIn("private", li.load_life()[0][0])                          # 可撤回 private
        with self.assertRaises(SystemExit):                                         # --event-id 不配数组
            li.main(["add", "--json", json.dumps([e]), "--event-id", "x", "--no-git"])
        with self.assertRaises(ValueError):
            li.validate({**e, "private": "yes"})

