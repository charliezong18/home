import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: F401,E402
import ledger  # noqa: E402


class LedgerTest(unittest.TestCase):
    def test_parse_skips_bad_lines(self):
        recs = ledger.parse_lines('{"id":"a"}\nnot json\n\n[1]\n{"noid":1}\n{"id":"b"}\n')
        self.assertEqual([r["id"] for r in recs], ["a", "b"])

    def test_merge_later_wins_and_keeps_created(self):
        m = ledger.merge([{"id": "a", "state": "pending", "ts": "t1"}, {"id": "b"},
                          {"id": "a", "state": "applied", "ts": "t2"}])
        self.assertEqual(list(m), ["a", "b"])
        self.assertEqual((m["a"]["state"], m["a"]["ts"], m["a"]["created"]), ("applied", "t2", "t1"))

    def test_append_adds_missing_newline(self):
        p = os.path.join(tempfile.mkdtemp(), "x.jsonl")
        with open(p, "w") as f:
            f.write('{"id":"a"}')        # 网页 PUT 可能留下没换行的末行
        ledger.append(p, {"id": "b"})
        self.assertEqual(list(ledger.read_ledger(p)), ["a", "b"])

    def test_normalize_edit(self):
        e = ledger.normalize_edit({"target": "p:x", "fields": {"next": "n"}}, "2026-10-28T00:00:00Z")
        self.assertEqual((e["state"], e["from"], e["ts"]), ("pending", "claude", "2026-10-28T00:00:00Z"))
        self.assertTrue(e["id"].startswith("e:"))
        for bad in ({}, {"target": "p:x"}, {"target": "p:x", "fields": {"next": "n"}, "from": "web"}, []):
            with self.assertRaises(ValueError):
                ledger.normalize_edit(bad, "t")

    def test_state_filters(self):
        recs = ledger.merge([{"id": "a", "state": "pending"}, {"id": "b", "state": "needs_hermes"},
                             {"id": "c", "state": "done"}])
        self.assertEqual([e["id"] for e in ledger.pending_edits(recs)], ["a"])
        self.assertEqual([e["id"] for e in ledger.notes_pending(recs)], ["b"])

    def test_find_reading_by_rid(self):
        recs = ledger.merge([{"id": "link-20261013-b559"}])
        self.assertIsNotNone(ledger.find_reading(recs, "link-20261013-b559"))
        self.assertIsNotNone(ledger.find_reading(recs, "r:20261013-b559"))
        self.assertIsNone(ledger.find_reading(recs, "r:nope"))


if __name__ == "__main__":
    unittest.main()
