"""collect_gtasks 单测：全部用虚构的 gog 输出夹具，不碰网络、不调真实 gog、不写快照。"""
import datetime as dt
import json
import subprocess
import unittest
from unittest import mock

import collect_gtasks as g

TODAY = dt.date(2026, 10, 31)


def task(i, title="示例任务", due=None, status="needsAction", **kw):
    t = {"id": f"t{i}", "title": title, "status": status, "notes": "不该出现的备注", "updated": "2026-10-30T00:00:00.000Z", **kw}
    if due:
        t["due"] = f"{due}T00:00:00.000Z"
    return t


class FakeGog:
    """按 --page 返回预置页；记录每次调用的命令。"""

    def __init__(self, pages, rc=0, stderr="", exc=None):
        self.pages, self.rc, self.stderr, self.exc, self.calls = pages, rc, stderr, exc, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if self.exc:
            raise self.exc
        page = next((a.split("=", 1)[1] for a in cmd if a.startswith("--page=")), None)
        body = self.pages[page]
        return subprocess.CompletedProcess(cmd, self.rc, stdout=body if isinstance(body, str) else json.dumps(body), stderr=self.stderr)


class FetchTest(unittest.TestCase):
    def test_pagination(self):
        fake = FakeGog({
            None: {"tasks": [task(1), task(2)], "nextPageToken": "P2"},
            "P2": {"tasks": [task(3)], "nextPageToken": "P3"},
            "P3": {"tasks": [task(4)]},
        })
        raw = g.fetch(fake)
        self.assertEqual([t["id"] for t in raw], ["t1", "t2", "t3", "t4"])
        self.assertEqual(len(fake.calls), 3)
        self.assertIn("--page=P3", fake.calls[2])
        for c in fake.calls:   # 只读：只许 list 子命令
            self.assertEqual(c[:3], ["gog", "tasks", "list"])
            self.assertIn("--json", c)

    def test_bare_array_and_other_envelope_key(self):
        self.assertEqual(len(g.fetch(FakeGog({None: [task(1), task(2)]}))), 2)
        self.assertEqual(len(g.fetch(FakeGog({None: {"results": [task(1)], "nextPageToken": ""}}))), 1)
        self.assertEqual(g.fetch(FakeGog({None: {"nextPageToken": None}})), [])

    def test_runaway_pagination_stops(self):
        with self.assertRaisesRegex(RuntimeError, "分页超过"):
            g.fetch(FakeGog({None: {"tasks": [], "nextPageToken": "X"}, "X": {"tasks": [], "nextPageToken": "X"}}))

    def test_gog_failures(self):
        with self.assertRaisesRegex(RuntimeError, "rc=1"):
            g.fetch(FakeGog({None: {}}, rc=1, stderr="auth expired"))
        with self.assertRaisesRegex(RuntimeError, "超时"):
            g.fetch(FakeGog({}, exc=subprocess.TimeoutExpired("gog", 30)))
        with self.assertRaisesRegex(RuntimeError, "起不来"):
            g.fetch(FakeGog({}, exc=FileNotFoundError("gog")))
        with self.assertRaisesRegex(RuntimeError, "解析失败"):
            g.fetch(FakeGog({None: "not json{"}))


class OpenTasksTest(unittest.TestCase):
    def test_completed_filtered_and_notes_dropped(self):
        raw = [task(1, due="2026-10-31"), task(2, status="completed", completed="2026-10-30T01:00:00.000Z"),
               task(3, deleted=True), task(4, hidden=True), task(5), {"title": "无 id"}, task(6, due="坏日期")]
        out = g.open_tasks(raw)
        self.assertEqual([t["id"] for t in out], ["t1", "t5", "t6"])
        self.assertEqual(out[0], {"id": "t1", "title": "示例任务", "due": "2026-10-31"})
        self.assertIsNone(out[2]["due"])   # 坏日期当无截止
        self.assertTrue(all(set(t) == {"id", "title", "due"} for t in out))


class TodosTest(unittest.TestCase):
    def test_sections_sort_and_contract(self):
        ts = g.open_tasks([task(1, "今天甲", "2026-10-31"), task(2, "过期新", "2026-10-30"), task(3, "过期旧", "2026-09-01"),
                           task(4, "明天", "2026-11-01"), task(5, "无日期")])
        out = g.todos(ts, TODAY, synced_at="2026-10-31T19:00:00Z")
        self.assertEqual(set(out), {"source", "list", "synced_at", "error", "today", "overdue", "nodate", "counts"})
        self.assertEqual((out["source"], out["list"], out["error"]), ("google-tasks", "My Tasks", None))
        self.assertEqual([t["title"] for t in out["today"]], ["今天甲"])
        self.assertEqual([t["due"] for t in out["overdue"]], ["2026-09-01", "2026-10-30"])   # due 升序
        self.assertEqual(out["nodate"], [{"id": "t5", "title": "无日期", "due": None}])
        self.assertEqual(out["counts"], {"today": 1, "overdue": 2, "nodate": 1})   # 未来到期的不进任何一段
        self.assertNotIn("notes", json.dumps(out, ensure_ascii=False))

    def test_title_truncated_and_empty(self):
        ts = g.open_tasks([task(1, "字" * 200, "2026-10-31"), task(2, "  ")])
        out = g.todos(ts, TODAY)
        self.assertEqual(len(out["today"][0]["title"]), 80)
        self.assertEqual(out["nodate"][0]["title"], "（无标题）")

    def test_nodate_capped_counts_real_total(self):
        out = g.todos(g.open_tasks([task(i) for i in range(25)]), TODAY)
        self.assertEqual(len(out["nodate"]), 20)
        self.assertEqual(out["counts"]["nodate"], 25)

    def test_timezone_boundary(self):
        # 洛杉矶 10-31 23:30（UTC 已是 11-01 06:30）：due 10-31 仍是「今天」，不算过期
        late = dt.datetime(2026, 11, 1, 6, 30, tzinfo=dt.timezone.utc)
        self.assertEqual(g.la_today(late), dt.date(2026, 10, 31))
        ts = g.open_tasks([task(1, due="2026-10-31"), task(2, due="2026-11-01")])
        out = g.todos(ts, g.la_today(late))
        self.assertEqual([t["id"] for t in out["today"]], ["t1"])
        self.assertEqual(out["overdue"], [])
        # 过了洛杉矶午夜（UTC 11-01 07:30 = PDT 00:30）：10-31 变过期，11-01 变今天
        out = g.todos(ts, g.la_today(dt.datetime(2026, 11, 1, 7, 30, tzinfo=dt.timezone.utc)))
        self.assertEqual([t["id"] for t in out["today"]], ["t2"])
        self.assertEqual([t["id"] for t in out["overdue"]], ["t1"])


class FromSnapshotTest(unittest.TestCase):
    SNAP = {"fetched": "2026-10-31T18:00:00Z", "data": {"tasks": [{"id": "t1", "title": "示例", "due": "2026-10-31"}]}}

    def test_never_ran(self):
        self.assertIsNone(g.from_snapshot(None, None, TODAY))
        self.assertIsNone(g.from_snapshot(None, {"consecutive_failures": 0}, TODAY))

    def test_ok(self):
        out = g.from_snapshot(self.SNAP, {"consecutive_failures": 0, "last_error_msg": "旧错"}, TODAY)
        self.assertIsNone(out["error"])
        self.assertEqual(out["synced_at"], "2026-10-31T18:00:00Z")
        self.assertEqual(out["counts"]["today"], 1)

    def test_failure_keeps_last_data(self):
        out = g.from_snapshot(self.SNAP, {"consecutive_failures": 2, "last_error_msg": "gtasks FAIL: gog rc=1"}, TODAY)
        self.assertEqual(out["error"], "gtasks FAIL: gog rc=1")
        self.assertEqual(out["counts"]["today"], 1)            # 上次成功的数据还在
        self.assertEqual(out["synced_at"], "2026-10-31T18:00:00Z")

    def test_failure_without_snapshot(self):
        out = g.from_snapshot(None, {"consecutive_failures": 1, "last_error_msg": ""}, TODAY)
        self.assertEqual(out["error"], "采集失败")
        self.assertEqual(out["counts"], {"today": 0, "overdue": 0, "nodate": 0})
        self.assertIsNone(out["synced_at"])


class MainTest(unittest.TestCase):
    def test_failure_exits_nonzero_without_snapshot(self):
        with mock.patch.object(g, "save_snapshot") as save, self.assertRaises(SystemExit) as cm, \
                mock.patch("sys.stderr"):
            g.main([], runner=FakeGog({None: {}}, rc=2, stderr="boom"))
        self.assertEqual(cm.exception.code, 1)
        save.assert_not_called()

    def test_ok_writes_snapshot_without_notes(self):
        with mock.patch.object(g, "save_snapshot") as save, mock.patch.object(g, "log"):
            self.assertEqual(g.main([], runner=FakeGog({None: {"tasks": [task(1, due="2026-10-31")]}})), 0)
        source, data = save.call_args.args
        self.assertEqual(source, "gtasks")
        self.assertNotIn("notes", json.dumps(data, ensure_ascii=False))

    def test_dry_run_prints_counts_only(self):
        with mock.patch.object(g, "save_snapshot") as save, mock.patch("builtins.print") as pr:
            g.main(["--dry-run"], runner=FakeGog({None: {"tasks": [task(1, "机密标题")]}}))
        save.assert_not_called()
        line = pr.call_args.args[0]
        self.assertIn("nodate=1", line)
        self.assertNotIn("机密标题", line)


if __name__ == "__main__":
    unittest.main()
