"""todos 分段（today / overdue / nodate）的时区性质测试：洛杉矶午夜、夏令时切换（2026-03-08 / 2026-11-01）。

Google Tasks 的 due 只有日期（API 存成 YYYY-MM-DDT00:00:00.000Z），取前 10 位、不做时区换算；只有「今天」按洛杉矶算。
全部是虚构任务，固定随机种子，不联网。
"""
import datetime as dt
import random
import unittest

import build_home
import collect_gtasks as g

UTC = dt.timezone.utc


def utc(*a):
    return dt.datetime(*a, tzinfo=UTC)


def la_offset_hours(t):
    """2026 年洛杉矶：03-08 10:00Z 起 PDT（-7），11-01 09:00Z 起 PST（-8）。独立于 zoneinfo 的参照实现。"""
    return -7 if utc(2026, 3, 8, 10) <= t < utc(2026, 11, 1, 9) else -8


def raw_task(i, due=None):
    t = {"id": f"t{i}", "title": f"虚构任务 {i}", "status": "needsAction", "notes": "不该出现"}
    if due:
        t["due"] = f"{due}T00:00:00.000Z"                      # gog / Tasks API 的原样格式
    return t


class LaTodayTest(unittest.TestCase):
    def test_matches_reference_every_7_minutes_around_both_dst_switches(self):
        for start, end in ((utc(2026, 3, 6), utc(2026, 3, 11)), (utc(2026, 10, 29), utc(2026, 11, 4))):
            t = start
            while t < end:
                want = (t + dt.timedelta(hours=la_offset_hours(t))).date()
                self.assertEqual(g.la_today(t), want, t.isoformat())
                t += dt.timedelta(minutes=7)

    def test_midnight_edges(self):
        # (洛杉矶午夜的 UTC 时刻, 新的一天)：11-01 之前是 07:00Z，之后变成 08:00Z
        for midnight, day in ((utc(2026, 10, 31, 7), dt.date(2026, 10, 31)),
                              (utc(2026, 11, 1, 7), dt.date(2026, 11, 1)),      # DST 当天的午夜仍是 PDT
                              (utc(2026, 11, 2, 8), dt.date(2026, 11, 2)),      # 第一个 PST 午夜
                              (utc(2026, 11, 3, 8), dt.date(2026, 11, 3)),
                              (utc(2026, 3, 8, 8), dt.date(2026, 3, 8)),
                              (utc(2026, 3, 9, 7), dt.date(2026, 3, 9))):
            self.assertEqual(g.la_today(midnight - dt.timedelta(seconds=1)), day - dt.timedelta(days=1), midnight)
            self.assertEqual(g.la_today(midnight), day, midnight)
        # 硬编码 -7 的错误会在这里暴露：11-02 07:30Z = 11-01 23:30 PST
        self.assertEqual(g.la_today(utc(2026, 11, 2, 7, 30)), dt.date(2026, 11, 1))

    def test_repeated_hour_is_same_day(self):
        # 11-01 01:30 出现两次（08:30Z PDT、09:30Z PST），都是 11-01
        self.assertEqual(g.la_today(utc(2026, 11, 1, 8, 30)), dt.date(2026, 11, 1))
        self.assertEqual(g.la_today(utc(2026, 11, 1, 9, 30)), dt.date(2026, 11, 1))

    def test_accepts_non_utc_aware_now(self):
        tokyo = dt.timezone(dt.timedelta(hours=9))
        self.assertEqual(g.la_today(dt.datetime(2026, 11, 2, 16, 30, tzinfo=tokyo)), dt.date(2026, 11, 1))


class BucketPropertyTest(unittest.TestCase):
    """随机任务集 × 随机「现在」（集中在 DST 前后）：分段是一个正确的划分。"""

    def check(self, raw, now):
        today = g.la_today(now)
        tasks = g.open_tasks(raw)
        out = g.todos(tasks, today)
        iso = today.isoformat()
        by_id = {t["id"]: t for t in tasks}
        ids = [x["id"] for k in ("today", "overdue", "nodate") for x in out[k]]
        self.assertEqual(len(ids), len(set(ids)), "同一任务出现在两段")
        for x in out["today"]:
            self.assertEqual(x["due"], iso)
        for x in out["overdue"]:
            self.assertLess(x["due"], iso)
        self.assertEqual([x["due"] for x in out["overdue"]], sorted(x["due"] for x in out["overdue"]))
        for x in out["nodate"]:
            self.assertIsNone(x["due"])
        # 每个到期 ≤ 今天的任务都在 today / overdue 里；未来的都不在
        want_today = {i for i, t in by_id.items() if t["due"] == iso}
        want_over = {i for i, t in by_id.items() if t["due"] and t["due"] < iso}
        want_nodate = {i for i, t in by_id.items() if not t["due"]}
        self.assertEqual({x["id"] for x in out["today"]}, want_today)
        self.assertEqual({x["id"] for x in out["overdue"]}, want_over)
        self.assertTrue({x["id"] for x in out["nodate"]} <= want_nodate)
        self.assertEqual(out["counts"], {"today": len(want_today), "overdue": len(want_over),
                                         "nodate": len(want_nodate)})
        self.assertEqual(len(out["nodate"]), min(len(want_nodate), g.NODATE_MAX))
        # due 原样是日期，不随时区漂移
        for x in out["today"] + out["overdue"]:
            self.assertEqual(x["due"], by_id[x["id"]]["due"])
        return out

    def test_random_sets_around_dst(self):
        rng = random.Random(20261101)
        base = utc(2026, 10, 30)
        for _ in range(400):
            now = base + dt.timedelta(minutes=rng.randrange(0, 5 * 24 * 60))
            raw = []
            for i in range(rng.randrange(0, 30)):
                if rng.random() < 0.25:
                    raw.append(raw_task(i))
                else:
                    raw.append(raw_task(i, (dt.date(2026, 10, 28) + dt.timedelta(days=rng.randrange(0, 9))).isoformat()))
            self.check(raw, now)

    def test_due_is_date_only_not_shifted(self):
        """due 11-01T00:00:00Z 在洛杉矶是 10-31 17:00，但它就是「11-01 到期」：整个洛杉矶 11-01（25 小时）都是今天。"""
        raw = [raw_task(1, "2026-11-01"), raw_task(2, "2026-10-31"), raw_task(3, "2026-11-02")]
        t = utc(2026, 11, 1, 7)                                     # 11-01 00:00 PDT
        end = utc(2026, 11, 2, 8)                                   # 11-02 00:00 PST
        self.assertEqual(end - t, dt.timedelta(hours=25))
        while t < end:
            out = self.check(raw, t)
            self.assertEqual(([x["id"] for x in out["today"]], [x["id"] for x in out["overdue"]]), (["t1"], ["t2"]), t)
            t += dt.timedelta(minutes=10)
        out = self.check(raw, end)
        self.assertEqual(([x["id"] for x in out["today"]], [x["id"] for x in out["overdue"]]), (["t3"], ["t2", "t1"]))

    def test_odd_due_values_do_not_crash(self):
        raw = [{"id": "a", "due": 20261101}, {"id": "b", "due": None}, {"id": "c", "due": ""},
               {"id": "d", "due": "2026-11-01"}, {"id": "e", "due": "2026-13-01T00:00:00.000Z"}]
        due = {t["id"]: t["due"] for t in g.open_tasks(raw)}
        self.assertIn(due.pop("a"), (None, "2026-11-01"))          # 3.11+ 的 fromisoformat 认 YYYYMMDD 基本格式
        self.assertEqual(due, {"b": None, "c": None, "d": "2026-11-01", "e": None})


class FailedTodosTest(unittest.TestCase):
    NOW = utc(2026, 11, 2, 12)

    def recs(self, ts):
        return [{"id": "f1", "op": "complete", "task_id": "T1", "state": "pending", "ts": "2026-11-02T10:00:00Z"},
                {"id": "f1", "state": "failed", "ts": ts, "error": "gog done rc=1: boom"}]

    def test_window_crosses_dst_in_real_hours(self):
        # 24 h 是真实小时，不是「同一个钟点」：11-01 12:00Z 进窗口，11-01 11:59Z 不进
        self.assertEqual(len(build_home.failed_todos(self.recs("2026-11-01T12:00:00Z"), self.NOW)), 1)
        self.assertEqual(build_home.failed_todos(self.recs("2026-11-01T11:59:59Z"), self.NOW), [])

    def test_bad_or_naive_ts_does_not_crash_build(self):
        """todo-actions.jsonl 经 GitHub 可被手改 / 客户端写坏：一行不带时区的 ts 不能让整个 build 抛异常。"""
        for ts in ("2026-11-02T10:00:05", "2026-11-02 10:00:05", "坏", None, 123):
            out = build_home.failed_todos(self.recs(ts), self.NOW)
            self.assertIsInstance(out, list, ts)
        self.assertEqual(len(build_home.failed_todos(self.recs("2026-11-02T10:00:05"), self.NOW)), 1)  # 无时区按 UTC
        self.assertEqual(len(build_home.failed_todos(self.recs("2026-11-02T02:00:05-08:00"), self.NOW)), 1)


if __name__ == "__main__":
    unittest.main()
