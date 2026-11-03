"""apply_edits worker：一条坏 edit 不拖垮整批（H3）。全在临时目录，不碰真实 vault。"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apply_edits  # noqa: E402
import collect_dashboard as cd  # noqa: E402

CARD = """---
id: p:demo
title: 示例卡
status: active
next: old
updated: 2026-10-01
---
## 现状
示例

## 里程碑
- [ ] 第一步
"""


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        dash = os.path.join(self.tmp.name, "Dashboard")
        os.makedirs(dash)
        self.card = os.path.join(dash, "demo.md")
        with open(self.card, "w", encoding="utf-8") as f:
            f.write(CARD)
        p = mock.patch.object(cd, "VAULT_DASH", dash)
        p.start()
        self.addCleanup(p.stop)

    def run_worker(self, edits):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(edits))), mock.patch.object(sys, "stdout", out):
            apply_edits.worker()
        return {x["id"]: x for x in json.loads(out.getvalue())}

    def test_malformed_edits_fail_alone_and_good_ones_apply(self):
        res = self.run_worker([
            {"id": "e:badnote", "target": "p:demo", "fields": {"next": "x"}, "note": {"oops": 1}},
            {"id": "e:badtarget", "target": {"p": "demo"}, "fields": {"next": "y"}},
            {"id": "e:listtarget", "target": ["p:demo"], "fields": {"next": "y"}},
            {"id": "e:badms", "target": "p:demo", "milestones": [{"text": "第一步", "done": True}], "note": 5},
            {"id": "e:good", "target": "p:demo", "fields": {"next": "new"}},
        ])
        for bad in ("e:badnote", "e:badtarget", "e:listtarget", "e:badms"):
            self.assertEqual(res[bad]["state"], "failed", bad)
        self.assertEqual(res["e:good"]["state"], "applied")
        with open(self.card, encoding="utf-8") as f:
            self.assertIn("next: new", f.read())

    def test_unexpected_exception_in_one_edit_is_contained(self):
        real = apply_edits.apply_card
        def flaky(path, e, today):
            if e["id"] == "e:boom":
                raise KeyError("boom")
            return real(path, e, today)
        with mock.patch.object(apply_edits, "apply_card", flaky):
            res = self.run_worker([{"id": "e:boom", "target": "p:demo", "fields": {"next": "a"}},
                                   {"id": "e:ok", "target": "p:demo", "fields": {"next": "b"}}])
        self.assertEqual(res["e:boom"]["state"], "failed")
        self.assertIn("KeyError", res["e:boom"]["error"])
        self.assertEqual(res["e:ok"]["state"], "applied")

    def test_validate_rejects_bad_types(self):
        self.assertIn("note", apply_edits.validate({"target": "p:x", "note": ["a"]}))
        self.assertIn("target", apply_edits.validate({"target": 3}))
        self.assertIn("target", apply_edits.validate({"fields": {"next": "a"}}))
        self.assertIsNone(apply_edits.validate({"target": "p:x", "note": None, "fields": {"next": "a"}}))


if __name__ == "__main__":
    unittest.main()
