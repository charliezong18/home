import datetime as dt
import unittest

from build_home import due_items
from collect_backlog import find_date, parse_backlog
from collect_countdown import next_date, parse_countdown

TODAY = dt.date(2026, 10, 27)

COUNTDOWN = """# x
- 2021-11-14 🔁 | 💑 示例纪念日
- 04-18 🔁 | 🎂 示例生日
- 2026-06-25 | 🎯 示例面试
- 02-29 🔁 | 🎂 闰日
- 坏行 | 没日期
- 13-40 | 非法日期
"""

BACKLOG = """## ⏰ 有窗口的（x）

| # | 事项 | 领域 | 谁干 | 窗口 / 备注 |
|---|---|---|---|---|
| ~~1~~ | ~~**划线**~~ | 旅行 | ☁️ | ✅ 结案 2026-07-22 |
| 3 | **核对 示例核对行程** | 旅行 | 👤 | 遗留核对项 |
| 5 | **续 示例续护照** | 杂 | 👤 | 窗口 2026-11-20 前 |
| 6 | **报税** | 杂 | 👤 | 11-30 前 |
| 7 | **订位** | 杂 | 👤 | 12月5日 截止 |

> 说明 2026-01-01

## 📋 主表

| 8 | **别处** | x | x | 2026-11-01 |
"""


class CountdownTest(unittest.TestCase):
    def test_parse(self):
        items = parse_countdown(COUNTDOWN)
        self.assertEqual([i["label"] for i in items], ["💑 示例纪念日", "🎂 示例生日", "🎯 示例面试", "🎂 闰日"])
        self.assertTrue(items[0]["recur"] and items[0]["year"] == 2021)
        self.assertIsNone(items[1]["year"])
        self.assertFalse(items[2]["recur"])

    def test_next_date(self):
        items = parse_countdown(COUNTDOWN)
        self.assertEqual(next_date(items[0], TODAY), dt.date(2026, 11, 14))
        self.assertEqual(next_date(items[1], TODAY), dt.date(2027, 4, 18))   # 今年已过 → 明年
        self.assertEqual(next_date(items[1], dt.date(2026, 4, 18)), dt.date(2026, 4, 18))   # 当天算
        self.assertEqual(next_date(items[2], TODAY), dt.date(2026, 6, 25))
        self.assertEqual(next_date(items[3], TODAY), dt.date(2027, 2, 28))   # 平年 2-29 → 2-28


class BacklogTest(unittest.TestCase):
    def test_find_date_formats(self):
        self.assertEqual(find_date("窗口 2026-11-20 前", TODAY), "2026-11-20")
        self.assertEqual(find_date("11-30 前", TODAY), "2026-11-30")
        self.assertEqual(find_date("12月5日 截止", TODAY), "2026-12-05")
        self.assertEqual(find_date("1月3日", TODAY), "2027-01-03")   # 已过 >14 天 → 明年
        self.assertEqual(find_date("无日期", TODAY), "")
        self.assertEqual(find_date("13-45 然后 2026-12-01", TODAY), "2026-12-01")

    def test_parse(self):
        items = parse_backlog(BACKLOG, TODAY)
        self.assertEqual([i["title"] for i in items], ["核对 示例核对行程", "续 示例续护照", "报税", "订位"])
        self.assertEqual([i["date"] for i in items], ["", "2026-11-20", "2026-11-30", "2026-12-05"])


class DueItemsTest(unittest.TestCase):
    def snaps(self):
        return {
            "countdown": {"data": {"items": parse_countdown(COUNTDOWN)}},
            "backlog": {"data": {"items": parse_backlog(BACKLOG, TODAY), "url": "obsidian://open?vault=Demo&file=x"}},
        }

    def test_merge_filter_sort(self):
        due = due_items(self.snaps(), today=TODAY)
        self.assertEqual([d["date"] for d in due], ["2026-11-14", "2026-11-20", "2026-11-30", "2026-12-05", ""])
        self.assertEqual(due[0]["id"], "d:countdown-示例纪念日")
        self.assertTrue(due[0]["recur"] and due[0]["note"] == "🔁 年度")
        self.assertEqual(due[-1]["title"], "核对 示例核对行程")   # 无日期保留并排最后
        self.assertTrue(all(d["id"].startswith("d:") for d in due))
        self.assertEqual(due[1]["source"], "backlog")
        self.assertTrue(due[1]["url"].startswith("obsidian:"))

    def test_window_filter(self):
        due = due_items(self.snaps(), today=TODAY)
        ids = [d["id"] for d in due]
        self.assertNotIn("d:countdown-示例生日", ids)   # 2027-04-18 超出 +60
        self.assertNotIn("d:countdown-google-onsite", ids)   # 2026-06-25 早于 -14

    def test_dedupe_later_wins(self):
        life = [{"id": "d:countdown-示例纪念日", "title": "覆盖", "date": "2026-11-15", "note": "", "source": "calendar"}]
        due = due_items(self.snaps(), life=life, today=TODAY)
        hit = [d for d in due if d["id"] == "d:countdown-示例纪念日"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["date"], "2026-11-15")

    def test_missing_snapshots(self):
        self.assertEqual(due_items({}, today=TODAY), [])


if __name__ == "__main__":
    unittest.main()
