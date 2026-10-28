import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402,F401
import events  # noqa: E402


class FakeApns:
    configured = True

    def __init__(self, results=None):
        self.results, self.sent = results or {}, []

    def send(self, device, payload, key):
        self.sent.append((device["token"], payload, key))
        return self.results.get(device["token"], (200, ""))


class EventsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="wp11-ev-")
        os.environ["DIBAO_STATE_DIR"] = os.path.join(tmp, "state")
        self.devices = os.path.join(tmp, "devices.jsonl")
        self.ntfy_calls, self.dead = [], []
        self.write_devices("t1")

    def write_devices(self, *tokens):
        with open(self.devices, "w") as f:
            for t in tokens:
                f.write(json.dumps({"token": t, "env": "sandbox", "active": True}) + "\n")

    def notifier(self, fake=None, ntfy_ok=True):
        def ntfy(text, url):
            self.ntfy_calls.append((text, url))
            return ntfy_ok
        self.fake = fake or FakeApns()
        return events.Notifier(self.fake, self.devices, ntfy=ntfy, on_deactivate=lambda t, r: self.dead.append((t, r)))

    @staticmethod
    def home(folds=(), sources=None):
        return {"folders": {"oldest": {"你拍": [{"n": n, "title": f"折{n}"} for n in folds]}},
                "sources": sources or {}}

    def test_new_fold_pushed_once_baseline_silent(self):
        n = self.notifier()
        self.assertEqual(n.scan_home(self.home([1, 2])), [])                  # 首次：静默基线
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(n.scan_home(self.home([1, 2, 7])), ["dup", "dup", "apns"])
        (_, payload, key), = self.fake.sent
        self.assertEqual(key, "fold-7")
        self.assertEqual(payload["url"], "menxia://?pr=7")
        self.assertEqual(payload["aps"]["alert"]["body"], "#7 折7")
        self.assertEqual(n.scan_home(self.home([1, 2, 7])), ["dup", "dup", "dup"])
        self.assertEqual(len(self.fake.sent), 1)
        # 重启后仍记得
        n2 = self.notifier()
        self.assertEqual(n2.scan_home(self.home([1, 2, 7])), ["dup", "dup", "dup"])
        self.assertEqual(self.ntfy_calls, [])

    def test_note_done_dedup_and_url(self):
        n = self.notifier()
        self.assertEqual(n.note_done("e-1", "某项目卡", "p:demo"), "apns")
        self.assertEqual(n.note_done("e-1", "某项目卡", "p:demo"), "dup")
        _, payload, key = self.fake.sent[0]
        self.assertEqual((key, payload["url"]), ("note-e-1", "menxia://home?id=p:demo"))
        self.assertEqual(payload["aps"]["alert"]["body"], "附言已处理：某项目卡")

    def test_failure_event_daily_apns_only(self):
        n = self.notifier()
        n.scan_home(self.home())                                                # 基线
        srcs = {"menxia": {"consecutive_failures": 3}, "dashboard": {"consecutive_failures": 2}}
        self.assertEqual(n.scan_home(self.home(sources=srcs)), ["apns"])
        self.assertEqual(n.scan_home(self.home(sources=srcs)), ["dup"])        # 同源同日 ≤1
        self.assertTrue(self.fake.sent[0][2].startswith("fail-menxia-"))
        self.assertEqual(self.ntfy_calls, [])

    def test_failure_event_without_devices_does_not_ntfy(self):
        n = self.notifier(FakeApns())
        self.write_devices()
        n.scan_home(self.home())
        self.assertEqual(n.scan_home(self.home(sources={"menxia": {"consecutive_failures": 5}})), ["none"])
        self.assertEqual(self.ntfy_calls, [])                                   # status.py 已经推过 ntfy
        self.assertNotIn("fail-", " ".join(n.pushed))                          # 没送达不记，之后能重试

    def test_due_today_only_and_dedup(self):
        n = self.notifier()
        home = {"projects": [{"id": "p:a", "title": "甲", "deadline": "2026-10-28"},
                             {"id": "p:b", "title": "乙", "deadline": "2026-10-29"}],
                "due": [{"id": "d:x", "title": "丙", "date": "2026-10-28"}, {"id": "d:y", "title": "丁", "date": ""}]}
        self.assertEqual(n.scan_due(home, "2026-10-28"), ["apns", "apns"])
        self.assertEqual(n.scan_due(home, "2026-10-28"), ["dup", "dup"])
        keys = [k for _, _, k in self.fake.sent]
        self.assertEqual(keys, ["due-p:a-2026-10-28", "due-d:x-2026-10-28"])
        self.assertEqual(self.fake.sent[0][1]["aps"]["alert"]["body"], "今天截止：甲")
        self.assertEqual(n.scan_due(home, "2026-10-29"), ["apns"])             # 次日换键

    def test_ntfy_fallback_when_no_active_device(self):
        n = self.notifier()
        self.write_devices()
        self.assertEqual(n.note_done("e-2", "卡", "p:z"), "ntfy")
        self.assertEqual(self.ntfy_calls, [("邸报：附言已处理：卡", "menxia://home?id=p:z")])
        self.assertEqual(self.fake.sent, [])

    def test_ntfy_fallback_when_all_apns_fail_and_dead_token_reported(self):
        self.write_devices("t1", "t2")
        n = self.notifier(FakeApns({"t1": (410, "Unregistered"), "t2": (400, "BadDeviceToken")}))
        self.assertEqual(n.note_done("e-3", "卡", "p:z"), "ntfy")
        self.assertEqual(self.dead, [("t1", "Unregistered"), ("t2", "BadDeviceToken")])
        self.assertEqual(len(self.ntfy_calls), 1)

    def test_apns_success_suppresses_ntfy_even_if_other_device_fails(self):
        self.write_devices("t1", "t2")
        n = self.notifier(FakeApns({"t2": (500, "InternalServerError")}))
        self.assertEqual(n.note_done("e-4", "卡", "p:z"), "apns")
        self.assertEqual(self.ntfy_calls, [])
        self.assertIn("500", n.health()["last_error"])

    def test_failed_delivery_not_recorded_so_retries(self):
        n = self.notifier(FakeApns({"t1": (500, "x")}), ntfy_ok=False)
        self.assertEqual(n.note_done("e-5", "卡", "p:z"), "failed")
        self.fake.results.clear()
        self.assertEqual(n.note_done("e-5", "卡", "p:z"), "apns")

    def test_dated_keys_expire_after_7_days_permanent_do_not(self):
        os.makedirs(os.path.dirname(events.state_path()), exist_ok=True)
        old = int(time.time()) - 8 * 86400
        with open(events.state_path(), "w") as f:
            for k in ("due-p:a-2026-01-01", "fold-3"):
                f.write(json.dumps({"key": k, "t": old}) + "\n")
        n = self.notifier()
        self.assertNotIn("due-p:a-2026-01-01", n.pushed)
        self.assertIn("fold-3", n.pushed)

    def test_send_test_and_health(self):
        n = self.notifier()
        self.assertEqual(n.send_test("测试", "门下推送", "menxia://home"), "apns")
        self.assertEqual(self.fake.sent[0][1]["url"], "menxia://home")
        h = n.health()
        self.assertEqual((h["configured"], h["devices"]), (True, 1))
        self.assertTrue(h["last_push"])


if __name__ == "__main__":
    unittest.main()
