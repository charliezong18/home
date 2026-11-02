"""早晚报（digest.py + Service.digest_tick / post_digest + cli digest --dry-run）。全部虚构数据、假发送器。"""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402,F401
import cli  # noqa: E402
import common  # noqa: E402
import config  # noqa: E402
import dibao  # noqa: E402
import digest  # noqa: E402
import events  # noqa: E402

UTC = dt.timezone.utc
DAY = "2026-11-02"                       # 周一，PST（UTC-8）


def utc(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


def fold(n, title, created="2026-10-20T18:00:00Z", **kw):
    return {"n": n, "title": title, "created": created, "proj": [], "qian": [], "deadline": "", **kw}


def sample_home():
    """虚构：两折待你拍（最老一折带 签:健康）、到期项含一条带金额、Google Tasks 计数。"""
    return {
        "folders": {
            "counts": {"你拍": 2, "你读": 1},
            "oldest": {
                "你拍": [fold(101, "体检报告复查", created="2026-10-23T17:00:00Z", qian=["健康"]),
                         fold(102, "示例：书架排序方案", created="2026-10-30T17:00:00Z", deadline="2026-11-04")],
                "你读": [fold(103, "示例读物：新到的", created="2026-11-03T04:00:00Z")],   # = 11/2 20:00 PST
            },
            "merged_recent": [
                {"n": 90, "title": "示例：首页改版", "merged_at": "2026-11-02T20:00:00Z", "qian": [], "proj": []},
                {"n": 91, "title": "签证材料清单", "merged_at": "2026-11-02T21:00:00Z", "qian": ["身份"], "proj": []},
                {"n": 92, "title": "昨天合并的", "merged_at": "2026-11-02T07:00:00Z", "qian": [], "proj": []},  # 11/1 23:00 PST
            ],
        },
        "projects": [{"id": "p:demo", "title": "示例项目", "deadline": "2026-11-02"}],
        "due": [{"id": "d:a", "title": "示例：交水费 $120", "date": "2026-11-02"},
                {"id": "d:b", "title": "示例：还书", "date": "2026-11-03"},
                {"id": "d:c", "title": "示例：过期的提醒", "date": "2026-10-30"},
                {"id": "d:d", "title": "太远的事", "date": "2026-11-20"}],
        "todos": {"counts": {"today": 2, "overdue": 1, "nodate": 4}},
    }


class FakeApns:
    configured = True

    def __init__(self, status=200):
        self.status, self.sent = status, []

    def send(self, device, payload, key):
        self.sent.append((payload, key))
        return self.status, ""


class ContentTest(unittest.TestCase):
    def test_morning_sections(self):
        d = digest.build("morning", sample_home(), DAY)
        self.assertEqual(d["title"], "早报 · 11/2 周一")
        self.assertEqual(d["url"], "menxia://home")
        self.assertEqual(d["key"], "digest-morning-2026-11-02")
        self.assertEqual(d["lines"], [
            "今天到期 2：示例项目，另 1 件私密事项",
            "3 天内到期 2：11/3 示例：还书、11/4 示例：书架排序方案",
            "待你拍 2 折，最老 1 件私密事项（10 天）",
            "过期待处理 1：示例：过期的提醒",
            "今日待办：今天到期 2，已过期 1",
        ])
        self.assertFalse(d["empty"])

    def test_evening_sections(self):
        d = digest.build("evening", sample_home(), DAY, done_todos=3)
        self.assertEqual(d["title"], "晚报 · 11/2 周一")
        self.assertEqual(d["lines"], [
            "今天进展：画可 2 折（示例：首页改版，另 1 件私密事项），完成待办 3 件",
            "今天新到 1 折：示例读物：新到的",
            "明天第一件事：示例：还书 到期",
        ])

    def test_evening_tomorrow_falls_back_to_oldest_fold(self):
        h = sample_home()
        h["due"] = []
        h["folders"]["oldest"]["你拍"] = [fold(102, "示例：书架排序方案")]
        self.assertEqual(digest.evening(h, DAY)[-1], "明天第一件事：拍 #102 示例：书架排序方案")

    def test_sensitive_never_shows_title(self):
        h = sample_home()
        h["folders"]["oldest"]["你拍"] = [fold(7, "某雇主谈判", proj=["雇主"]), fold(8, "报销 ¥300"),
                                         fold(9, "季度账单", qian=["钱"])]
        h["folders"]["counts"]["你拍"] = 3
        for kind in ("morning", "evening"):
            body = digest.build(kind, h, DAY)["body"]
            for secret in ("体检", "签证", "交水费", "$120", "雇主", "¥300", "季度账单", "#101", "#91", "#7 "):
                self.assertNotIn(secret, body, f"{kind} 泄露 {secret}")

    def test_money_detection(self):
        for t in ("付 $45", "退款 1,200 元", "3万首付", "¥88", "年费 99 USD"):
            self.assertTrue(digest.has_money(t), t)
        for t in ("示例：还书", "第 3 章", "2026 计划", "#12 改版"):
            self.assertFalse(digest.has_money(t), t)

    def test_empty_day_is_one_line(self):
        for kind, line in (("morning", "今天没有到期和待拍"), ("evening", "今天没有新进展，明天暂无安排")):
            d = digest.build(kind, {}, DAY)
            self.assertTrue(d["empty"])
            self.assertEqual(d["body"], line)
            self.assertNotIn("\n", d["body"])

    def test_listing_caps_titles(self):
        items = [{"title": f"事{i}", "private": False} for i in range(5)] + [{"title": "x", "private": True}]
        self.assertEqual(digest.listing(items), "事0、事1、事2 等 5 件，另 1 件私密事项")

    def test_todos_done_today_net_of_undo(self):
        recs = [
            {"id": "a1", "op": "complete", "task_id": "T1", "state": "pending", "ts": "2026-11-02T17:00:00Z"},
            {"id": "a1", "state": "done", "ts": "2026-11-02T17:00:05Z"},
            {"id": "a2", "op": "complete", "task_id": "T2", "state": "pending", "ts": "2026-11-02T18:00:00Z"},
            {"id": "a2", "state": "done", "ts": "2026-11-02T18:00:05Z"},
            {"id": "a3", "op": "undo", "task_id": "T2", "state": "pending", "ts": "2026-11-02T19:00:00Z"},
            {"id": "a3", "state": "done", "ts": "2026-11-02T19:00:05Z"},
            {"id": "a4", "op": "complete", "task_id": "T4", "state": "pending", "ts": "2026-11-03T09:00:00Z"},
            {"id": "a4", "state": "done", "ts": "2026-11-03T09:00:05Z"},          # 11/3 01:00 PST，不算 11/2
            {"id": "a5", "op": "complete", "task_id": "T5", "state": "pending", "ts": "2026-11-02T20:00:00Z"},
            {"id": "a5", "state": "failed", "ts": "2026-11-02T20:00:05Z", "error": "x"},
        ]
        self.assertEqual(digest.todos_done_today(recs, DAY), 1)


class WindowTest(unittest.TestCase):
    def test_morning_window(self):
        self.assertIsNone(digest.slot_day("morning", utc("2026-11-02T15:59:59")))      # 07:59:59 PST
        self.assertEqual(digest.slot_day("morning", utc("2026-11-02T16:00:00")), DAY)  # 08:00
        self.assertEqual(digest.slot_day("morning", utc("2026-11-02T16:29:59")), DAY)  # 08:29:59 补发
        self.assertIsNone(digest.slot_day("morning", utc("2026-11-02T16:30:00")))      # 08:30 起跳过

    def test_evening_window_crosses_utc_midnight(self):
        self.assertEqual(digest.slot_day("evening", utc("2026-11-03T05:30:00")), DAY)  # 11/2 21:30 PST = 11/3 UTC
        self.assertEqual(digest.slot_day("evening", utc("2026-11-03T05:59:00")), DAY)
        self.assertIsNone(digest.slot_day("evening", utc("2026-11-03T06:00:00")))
        self.assertIsNone(digest.slot_day("morning", utc("2026-11-03T05:30:00")))

    def test_dst_boundary(self):
        # 2026-11-01 02:00 PDT → PST：前一天 08:00 = 15:00Z，当天 08:00 = 16:00Z
        self.assertEqual(digest.slot_day("morning", utc("2026-10-31T15:00:00")), "2026-10-31")
        self.assertIsNone(digest.slot_day("morning", utc("2026-11-01T15:00:00")))      # 11/1 07:00 PST
        self.assertEqual(digest.slot_day("morning", utc("2026-11-01T16:10:00")), "2026-11-01")

    def test_la_date_of_utc_timestamp(self):
        self.assertEqual(digest.la_date("2026-11-03T04:00:00Z"), DAY)
        self.assertEqual(digest.la_date("bad"), "")


class ServiceDigestTest(unittest.TestCase):
    """Service.digest_tick：去重账本、补发窗口、送达失败重试。home.json / 账本都指向临时目录。"""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="digest-")
        self._saved = (dibao.HOME_JSON, dibao.TODO_ACTIONS, os.environ.get("DIBAO_STATE_DIR"))
        os.environ["DIBAO_STATE_DIR"] = os.path.join(tmp, "state")
        dibao.HOME_JSON = os.path.join(tmp, "home.json")
        dibao.TODO_ACTIONS = os.path.join(tmp, "todo-actions.jsonl")
        with open(dibao.HOME_JSON, "w") as f:
            json.dump(sample_home(), f, ensure_ascii=False)
        self.devices = os.path.join(tmp, "devices.jsonl")
        with open(self.devices, "w") as f:
            f.write(json.dumps({"token": "fake-token-1", "env": "sandbox"}) + "\n")
        self.ntfy = []
        self.svc = dibao.Service(config.load(), push=False)
        self.fake = FakeApns()
        self.svc.notifier = self.new_notifier()

    def tearDown(self):
        dibao.HOME_JSON, dibao.TODO_ACTIONS, sd = self._saved
        os.environ["DIBAO_STATE_DIR"] = sd or ""

    def new_notifier(self):
        return events.Notifier(self.fake, self.devices, ntfy=lambda t, u: self.ntfy.append((t, u)) or True)

    def test_sends_once_per_day_and_survives_restart(self):
        self.assertEqual(self.svc.digest_tick(utc("2026-11-02T16:00:30")), {"morning": "apns"})
        self.assertEqual(self.svc.digest_tick(utc("2026-11-02T16:05:00")), {})
        self.svc.notifier = self.new_notifier()                  # 模拟服务重启：从 pushed.jsonl 读回
        self.assertEqual(self.svc.digest_tick(utc("2026-11-02T16:10:00")), {})
        self.assertEqual(len(self.fake.sent), 1)
        payload, key = self.fake.sent[0]
        self.assertEqual(key, "digest-morning-2026-11-02")
        self.assertEqual(payload["url"], "menxia://home")
        self.assertEqual(payload["aps"]["alert"]["title"], "早报 · 11/2 周一")
        self.assertNotIn("体检", payload["aps"]["alert"]["body"])
        # 第二天照常发
        self.assertEqual(self.svc.digest_tick(utc("2026-11-03T16:00:00")), {"morning": "apns"})

    def test_missed_beyond_grace_is_skipped(self):
        self.assertEqual(self.svc.digest_tick(utc("2026-11-02T16:31:00")), {})       # 08:31 才醒
        self.assertEqual(self.svc.digest_tick(utc("2026-11-02T23:00:00")), {})
        self.assertEqual(self.fake.sent, [])

    def test_late_within_grace_is_sent(self):
        self.assertEqual(self.svc.digest_tick(utc("2026-11-03T05:55:00")), {"evening": "apns"})  # 21:55
        self.assertEqual(self.fake.sent[0][1], "digest-evening-2026-11-02")

    def test_failed_delivery_retries_within_window(self):
        self.fake.status = 500
        self.svc.notifier = events.Notifier(self.fake, self.devices, ntfy=lambda t, u: False)
        self.assertEqual(self.svc.digest_tick(utc("2026-11-02T16:00:00")), {"morning": "failed"})
        self.fake.status = 200
        self.assertEqual(self.svc.digest_tick(utc("2026-11-02T16:01:00")), {"morning": "apns"})
        self.assertEqual(self.svc.digest_tick(utc("2026-11-02T16:02:00")), {})

    def test_no_device_falls_back_to_ntfy(self):
        open(self.devices, "w").close()
        self.assertEqual(self.svc.digest_tick(utc("2026-11-02T16:00:00")), {"morning": "ntfy"})
        self.assertEqual(self.ntfy[0][1], "menxia://home")
        self.assertTrue(self.ntfy[0][0].startswith("早报 · 11/2 周一："))

    def test_post_digest_dry_run_does_not_send(self):
        code, out = self.svc.post_digest("morning", {"dry_run": True})
        self.assertEqual(code, 200)
        self.assertIn("body", out["digest"])
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(self.ntfy, [])


class CliDryRunTest(unittest.TestCase):
    def test_dry_run_prints_without_sending(self):
        tmp = tempfile.mkdtemp(prefix="digest-cli-")
        with open(os.path.join(tmp, "home.json"), "w") as f:
            json.dump(sample_home(), f, ensure_ascii=False)
        saved, common.DATA_DIR = common.DATA_DIR, tmp
        calls = []
        saved_call, cli.call = cli.call, lambda *a, **k: calls.append(a) or (200, {})
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["digest", "evening", "--dry-run"])
            lines = []
            cli.digest_dry_run("morning", now=utc("2026-11-02T16:00:00"), out=lines.append)
        finally:
            common.DATA_DIR, cli.call = saved, saved_call
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])                               # dry-run 不连服务
        self.assertIn("[dry-run] evening", buf.getvalue())
        self.assertTrue(lines[0].startswith("[dry-run] morning 2026-11-02  key=digest-morning-2026-11-02"))
        self.assertEqual(lines[1], "标题：早报 · 11/2 周一")
        self.assertIn("待你拍 2 折", lines[2])
        self.assertNotIn("体检", "\n".join(lines))

    def test_send_and_dry_run_are_exclusive(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                __import__("contextlib").redirect_stderr(io.StringIO()):
            cli.main(["digest", "morning", "--dry-run", "--send"])


if __name__ == "__main__":
    unittest.main()
