"""低优先级修复的回归测试：H8 过期阈值、H9 重采 gtasks 不持 svc 锁、H10 早报 / 今天截止跳过 done·ice 项目。"""
import datetime as dt
import os
import sys
import tempfile
import threading
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402
import build_home  # noqa: E402
import config  # noqa: E402
import dibao  # noqa: E402
import digest  # noqa: E402
import events  # noqa: E402
import todo_actions as ta  # noqa: E402
from test_todo_actions import FakeGog, action, write_remote_lines  # noqa: E402

UTC = dt.timezone.utc


class StaleThresholdTest(unittest.TestCase):
    """H8：健康时 generated 最多落后 HEARTBEAT_H + 一个采集周期；阈值必须更宽，否则 3–3.5h 窗口误报过期。"""

    def test_threshold_exceeds_heartbeat_plus_collect(self):
        collect_h = config.load().collect_secs / 3600
        self.assertGreater(digest.STALE_H, build_home.HEARTBEAT_H + collect_h)

    def test_heartbeat_window_not_stale(self):
        now = dt.datetime(2026, 11, 2, 16, 0, tzinfo=UTC)
        gen = (now - dt.timedelta(hours=3, minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNone(digest.stale_reason({"generated": gen}, now))
        old = (now - dt.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIn("小时未更新", digest.stale_reason({"generated": old}, now))


HOME = {"projects": [
    {"id": "p:live", "title": "进行中", "status": "active", "tier": "now", "deadline": "2026-10-20"},
    {"id": "p:done", "title": "已完成", "status": "done", "tier": "now", "deadline": "2026-10-20"},
    {"id": "p:ice", "title": "冷冻", "status": "ice", "deadline": "2026-10-20"},
    {"id": "p:icetier", "title": "冰层", "status": "active", "tier": "ice", "deadline": "2026-10-20"},
]}


class DoneIceFilterTest(unittest.TestCase):
    """H10：与网页 live 过滤一致。"""

    def test_dated_items_skip_done_and_ice(self):
        self.assertEqual([x["id"] for x in digest.dated_items(HOME)], ["p:live"])
        body = digest.build("morning", HOME, "2026-11-02")["body"]
        self.assertIn("进行中", body)
        for t in ("已完成", "冷冻", "冰层"):
            self.assertNotIn(t, body)

    def test_scan_due_skips_done_and_ice(self):
        tmp = tempfile.mkdtemp(prefix="h10-")
        os.environ["DIBAO_STATE_DIR"] = os.path.join(tmp, "state")
        sent = []

        class Apns:
            configured = True

            def send(self, device, payload, key):
                sent.append(key)
                return 200, ""
        devices = os.path.join(tmp, "devices.jsonl")
        with open(devices, "w") as f:
            f.write('{"token": "t1", "env": "sandbox", "active": true}\n')
        n = events.Notifier(Apns(), devices, ntfy=lambda *a: True)
        n.scan_due(HOME, "2026-10-20")
        self.assertEqual(sent, ["due-p:live-2026-10-20"])


if __name__ == "__main__":
    unittest.main()
