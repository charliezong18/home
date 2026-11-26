"""用量看板 R-REFETCH / R-REREAD：usage_transcripts 解析 + collect_usage 接线。全部用临时 fixture。"""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import collect_usage
import usage_transcripts as T
from test_usage import Fixture, SECRET, TZ

SID = "11111111-2222-3333-4444-555555555555"
FETCH = "mcp__plugin_context-mode_context-mode__ctx_fetch_and_index"


def use(name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": SECRET},
                                                         {"type": "tool_use", "name": name, "input": inp}]}}


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="usage-tr-")
        self.proj = os.path.join(self.root, "projects", "-some-proj")
        os.makedirs(self.proj)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, lines, sid=SID):
        p = os.path.join(self.proj, sid + ".jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for o in lines:
                f.write((o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)) + "\n")
        return p

    def test_refetch_counts_repeats_only(self):
        p = self.write([use("WebFetch", url="https://a", prompt=SECRET), use("WebFetch", url="https://a", prompt="x"),
                        use(FETCH, url="https://a"), use("WebFetch", url="https://b")])
        self.assertEqual(T.scan_transcript(p), {"refetch": 2, "reread": 0})      # a 共 3 次 → 多 2 次；b 只 1 次不算

    def test_reread_threshold_is_three_and_ignores_ranged_reads(self):
        p = self.write([use("Read", file_path="/x/a.md")] * 2 + [use("Read", file_path="/x/b.md")] * 3
                       + [use("Read", file_path="/x/c.md", offset=10, limit=50)] * 5)
        self.assertEqual(T.scan_transcript(p), {"refetch": 0, "reread": 2})      # a 2 次不算；b 3 次多 2；c 分段读不算

    def test_ignores_non_assistant_lines_blobs_and_garbage(self):
        blob = dict(use("WebFetch", url="https://a"), type="api-request-blob")
        p = self.write([blob, blob, blob, "not json {tool_use}", {"type": "user", "message": {"content": "x"}},
                        use("Bash", command="curl https://a"), use("WebFetch", url="https://a")])
        self.assertEqual(T.scan_transcript(p), {"refetch": 0, "reread": 0})

    def test_find_transcript_by_session_id(self):
        p = self.write([use("Read", file_path="/x")])
        self.assertEqual(T.find_transcript(SID, os.path.join(self.root, "projects")), p)
        self.assertIsNone(T.find_transcript("nope-0000", os.path.join(self.root, "projects")))
        self.assertIsNone(T.find_transcript("../../etc/passwd", os.path.join(self.root, "projects")))
        self.assertIsNone(T.find_transcript(None))
        self.assertIsNone(T.scan_task({"session_id": None}))
        self.assertEqual(T.scan_task({"session_id": SID}, os.path.join(self.root, "projects")), {"refetch": 0, "reread": 0})
        self.assertEqual(list(T.tool_calls(os.path.join(self.root, "missing.jsonl"))), [])


class WasteRuleTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.rows = self.fx.rows(since="2026-11-17", until="2026-11-23")
        self.now = dt.datetime(2026, 11, 23, 23, 0, tzinfo=TZ)

    def tearDown(self):
        self.fx.close()

    def day(self, scan):
        return collect_usage.build(self.rows, self.now.date(), self.now, scan=scan)["days"][0]

    def test_worker_rows_carry_session_id_only_when_present(self):
        self.assertTrue(all("session_id" in r for r in self.rows if r["source"] == "worker"))

    def test_rules_emitted_with_ids_and_no_cost(self):
        table = {"t-a": {"refetch": 3, "reread": 0}, "t-heavy": {"refetch": 1, "reread": 4}}
        w = {x["rule"]: x for x in self.day(lambda t: table.get(t["task_id"]))["waste"]}
        self.assertEqual((w["R-REFETCH"]["count"], w["R-REFETCH"]["ids"]), (4, ["t-heavy", "t-a"]))   # 按成本降序遍历
        self.assertEqual((w["R-REREAD"]["count"], w["R-REREAD"]["ids"]), (4, ["t-heavy"]))
        self.assertNotIn("cost_usd", w["R-REFETCH"])

    def test_no_scan_or_clean_transcripts_add_nothing(self):
        base = {"R-CRON-IDLE", "R-RESUME-HEAVY", "R-MAXTURNS", "R-DENY"}
        self.assertEqual({x["rule"] for x in self.day(None)["waste"]}, base)
        self.assertEqual({x["rule"] for x in self.day(lambda t: {"refetch": 0, "reread": 0})["waste"]}, base)
        self.assertEqual({x["rule"] for x in self.day(lambda t: None)["waste"]}, base)

    def test_task_carries_session_id_from_run_json(self):
        got = {}
        self.day(lambda t: got.setdefault(t["task_id"], t.get("session_id")))
        self.assertEqual(set(got), {"t-a", "menxia-b", "t-heavy", "t-old"})      # build 扫 30 天，t-old 在 11-22


if __name__ == "__main__":
    unittest.main()
