"""collectors/notify.py：topic 从 config.toml 读、过渡期双发、dry-run 不外发；仓库里不许再有 topic 字面量。"""
import os
import re
import tempfile
import unittest
from unittest import mock

import _env  # noqa: F401  （必须先于 notify）
import notify

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="notify-")
        self.cfg = os.path.join(self.dir, "config.toml")
        self.env = mock.patch.dict(os.environ, {"DIBAO_CONFIG": self.cfg, "AI_HOME_NTFY_DRYRUN": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.reqs = []

    def write(self, body):
        with open(self.cfg, "w") as f:
            f.write(body)

    def urlopen(self, statuses):
        def fake(req, timeout=None):
            self.reqs.append(req)
            s = statuses[len(self.reqs) - 1]
            if isinstance(s, Exception):
                raise s
            return FakeResp(s)
        return mock.patch("urllib.request.urlopen", side_effect=fake)

    def test_dual_send_new_then_legacy(self):
        self.write('[ntfy]\ntopic = "test-new-topic"\nlegacy_topic = "test-old-topic"\n')
        with self.urlopen([200, 200]):
            self.assertTrue(notify.send("你好", title="T", click="menxia://home"))
        self.assertEqual([r.full_url for r in self.reqs],
                         ["https://ntfy.sh/test-new-topic", "https://ntfy.sh/test-old-topic"])
        self.assertEqual(self.reqs[0].data, "你好".encode())
        self.assertEqual(self.reqs[0].get_header("Title"), "T")
        self.assertEqual(self.reqs[0].get_header("Click"), "menxia://home")

    def test_one_topic_failing_still_counts_as_delivered(self):
        self.write('[ntfy]\ntopic = "test-new-topic"\nlegacy_topic = "test-old-topic"\n')
        with self.urlopen([OSError("boom"), 200]):
            self.assertTrue(notify.send("x", title="T"))
        self.assertIsNone(self.reqs[0].get_header("Click"))

    def test_all_fail_returns_false(self):
        self.write('[ntfy]\ntopic = "test-new-topic"\nserver = "https://example.test/"\n')
        with self.urlopen([OSError("boom")]):
            self.assertFalse(notify.send("x", title="T"))
        self.assertEqual(self.reqs[0].full_url, "https://example.test/test-new-topic")

    def test_no_config_sends_nothing(self):
        with self.urlopen([]):
            self.assertFalse(notify.send("x", title="T"))
        self.assertEqual(self.reqs, [])

    def test_same_legacy_not_sent_twice(self):
        self.write('[ntfy]\ntopic = "test-same"\nlegacy_topic = "test-same"\n')
        self.assertEqual(notify.load_targets(), [("topic", "https://ntfy.sh/test-same")])

    def test_dryrun_never_hits_network(self):
        self.write('[ntfy]\ntopic = "test-new-topic"\n')
        os.environ["AI_HOME_NTFY_DRYRUN"] = "1"
        with self.urlopen([]):
            self.assertTrue(notify.send("x", title="T"))
        self.assertEqual(self.reqs, [])

    def test_no_topic_literal_in_repo(self):
        pat = re.compile(r"ntfy\.sh/[A-Za-z0-9_-]{3,}")
        hits = []
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in (".git", ".venv", ".scratch", "__pycache__")]
            for name in files:
                p = os.path.join(root, name)
                if p == os.path.abspath(__file__):
                    continue
                try:
                    with open(p, encoding="utf-8") as f:
                        if pat.search(f.read()):
                            hits.append(os.path.relpath(p, REPO))
                except (UnicodeDecodeError, OSError):
                    pass
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
