import unittest

import build_home
import collect_menxia as m


def pr(*labels):
    return {
        "number": 7, "title": "t", "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-02T00:00:00Z", "draft": False,
        "labels": [{"name": n} for n in labels],
    }


class FolderItemTest(unittest.TestCase):
    def test_due_valid(self):
        self.assertEqual(m.folder_item(pr("due:2026-11-05"))["deadline"], "2026-11-05")

    def test_due_invalid(self):
        self.assertEqual(m.folder_item(pr("due:11-05"))["deadline"], "")

    def test_due_earliest(self):
        it = m.folder_item(pr("due:2026-12-01", "due:2026-11-05"))
        self.assertEqual(it["deadline"], "2026-11-05")

    def test_no_due(self):
        it = m.folder_item(pr("wait:你拍"))
        self.assertEqual((it["deadline"], it["deadline_note"], it["wait"]), ("", "", "你拍"))

    def test_oldest_n(self):
        self.assertEqual(m.OLDEST_N, 20)


class SourceEntryTest(unittest.TestCase):
    def test_ok_clears_last_error(self):
        e = build_home.source_entry("menxia", {"consecutive_failures": 0, "last_error_msg": "old"}, None, "")
        self.assertTrue(e["ok"])
        self.assertEqual(e["last_error"], "")

    def test_fail_keeps_error(self):
        e = build_home.source_entry("menxia", {"consecutive_failures": 2, "last_error_msg": "boom"}, None, "")
        self.assertFalse(e["ok"])
        self.assertEqual(e["last_error"], "boom")


if __name__ == "__main__":
    unittest.main()
