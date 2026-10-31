"""build_home 读物账本坏行容错 + apply_edits --worker 真实落卡路径。全在临时目录里跑，不碰真实 home-data / vault。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from unittest import mock

import apply_edits
import build_home
import common
import life_ingest


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def reading(rid, title, **kw):
    return {"id": rid, "title": title, "url": f"https://example.com/{rid}", "added": "2026-10-28",
            "ts": "2026-10-28T00:00:00Z", "status": "new", **kw}


class BuildHomeReadingsTest(unittest.TestCase):
    """main() 走真实 git（临时仓）：只把 sync_down / push / 锁换掉。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = os.path.join(tmp.name, "data")
        self.cache = os.path.join(tmp.name, "cache")
        os.makedirs(self.cache)
        sh("git", "init", "-q", "-b", "home-data", self.data)
        sh("git", "-C", self.data, "config", "user.email", "t@example.com")
        sh("git", "-C", self.data, "config", "user.name", "t")
        sh("git", "-C", self.data, "commit", "-q", "--allow-empty", "-m", "init")
        self.ledger = os.path.join(self.data, "readings.jsonl")
        for target, name, value in [
            (common, "DATA_DIR", self.data), (common, "CACHE_DIR", self.cache),
            (build_home, "DATA_DIR", self.data), (build_home, "CACHE_DIR", self.cache),
            (build_home, "LEDGER", self.ledger), (life_ingest, "DATA_DIR", self.data),
            (build_home, "data_lock", nullcontext), (build_home, "sync_down", lambda: None),
            (build_home, "commit_and_push", self.commit_local),
        ]:
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def commit_local(self, paths, message):
        common.git("add", "--", *paths)
        if common.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        common.git("commit", "--quiet", "-m", message)
        return True

    def write_ledger(self, *lines):
        with open(self.ledger, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def home(self):
        with open(os.path.join(self.data, "home.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_load_readings_skips_bad_lines(self):
        self.write_ledger(json.dumps(reading("link-a", "A")), '{"id": "link-b", "title": "截断',
                          "[1, 2]", '{"title": "缺 id"}', json.dumps(reading("link-c", "C")),
                          json.dumps({"id": "link-c", "status": "done"}))
        items, bad = build_home.load_readings()
        self.assertEqual([r["id"] for r in items], ["link-a"])   # c 已 done，不展示
        self.assertEqual(bad, 3)

    def test_main_bad_line_keeps_readings(self):
        self.write_ledger(json.dumps(reading("link-a", "A")), '{"id": "link-b", "title": "截断',
                          json.dumps(reading("link-c", "C", ts="2026-10-28T01:00:00Z")))
        rc = build_home.main()
        self.assertEqual(rc, 3)                                   # 仍算降级，交给 status 记 readings 失败
        home = self.home()
        self.assertEqual([r["rid"] for r in home["readings"]], ["r:c", "r:a"])
        src = home["sources"]["readings"]
        self.assertFalse(src["ok"])
        self.assertIn("1 行无效", src["last_error"])
        self.assertIn("home.json", common.git("show", "--stat", "HEAD").stdout)   # 真提交了

    def test_main_clean_ledger_ok(self):
        self.write_ledger(json.dumps(reading("link-a", "A")))
        self.assertEqual(build_home.main(), 0)
        home = self.home()
        self.assertTrue(home["sources"]["readings"]["ok"])
        self.assertEqual([r["rid"] for r in home["readings"]], ["r:a"])
        self.assertNotIn("todos", home)   # 没有 gtasks 快照、也没失败过 → 不写 todos 键

    def test_main_todos_from_snapshot_with_error(self):
        self.write_ledger(json.dumps(reading("link-a", "A")))
        today = build_home.collect_gtasks.la_today().isoformat()
        common.write_json(os.path.join(self.cache, "snapshot-gtasks.json"), {"source": "gtasks", "fetched": "2026-10-31T18:00:00Z",
                          "data": {"tasks": [{"id": "t1", "title": "示例待办", "due": today}, {"id": "t2", "title": "示例无日期", "due": None}]}})
        common.write_json(os.path.join(self.cache, "status.json"),
                          {"gtasks": {"consecutive_failures": 1, "last_error_msg": "gtasks FAIL: gog rc=1"}})
        self.assertEqual(build_home.main(), 0)
        t = self.home()["todos"]
        self.assertEqual(t["counts"], {"today": 1, "overdue": 0, "nodate": 1})   # 失败仍保留上次成功的数据
        self.assertEqual(t["error"], "gtasks FAIL: gog rc=1")
        self.assertEqual(t["synced_at"], "2026-10-31T18:00:00Z")
        self.assertFalse(self.home()["sources"]["gtasks"]["ok"])

    def test_main_unreadable_ledger_keeps_previous_readings(self):
        self.write_ledger(json.dumps(reading("link-a", "A")))
        self.assertEqual(build_home.main(), 0)
        os.remove(self.ledger)
        os.mkdir(self.ledger)                                     # 读不出：IsADirectoryError
        self.assertEqual(build_home.main(), 3)
        home = self.home()
        self.assertEqual([r["rid"] for r in home["readings"]], ["r:a"])   # 沿用上一份，不写空列表
        self.assertFalse(home["sources"]["readings"]["ok"])


CARD = """---
id: p:test
title: 测试卡
status: active
next: old
updated: 2026-10-01
---
# 测试卡

## 里程碑
- [ ] 第一步
- [ ] 第二步

## 日志
- 2026-10-01 建卡
"""


class WorkerApplyCardTest(unittest.TestCase):
    """真起 apply_edits.py --worker 子进程（dibao 默认 worker 就是它），vault 指向临时 Dashboard。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dash = os.path.join(tmp.name, "Dashboard")
        os.makedirs(self.dash)
        self.card = os.path.join(self.dash, "测试卡.md")
        with open(self.card, "w", encoding="utf-8") as f:
            f.write(CARD)
        self.env = {**os.environ, "AI_HOME_DASHBOARD": self.dash, "AI_HOME_DATA": os.path.join(tmp.name, "data"),
                    "AI_HOME_CACHE": os.path.join(tmp.name, "cache"), "AI_HOME_NTFY_DRYRUN": "1"}

    def worker(self, edits):
        r = subprocess.run([sys.executable, apply_edits.__file__, "--worker"], input=json.dumps(edits),
                           capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return {x["id"]: x for x in json.loads(r.stdout)}

    def text(self):
        with open(self.card, encoding="utf-8") as f:
            return f.read()

    def test_fields_milestone_note_and_idempotent(self):
        edit = {"id": "e:1", "target": "p:test", "fields": {"next": "新的下一步", "status": "waiting"},
                "milestones": [{"text": "第一步", "done": True}], "note": "顺便看看"}
        out = self.worker([edit])["e:1"]
        self.assertEqual((out["state"], out["changed"]), ("needs_hermes", True))
        t = self.text()
        self.assertIn("next: 新的下一步\n", t)
        self.assertIn("status: waiting\n", t)
        self.assertIn("- [x] 第一步\n- [ ] 第二步", t)
        self.assertNotIn("updated: 2026-10-01", t)
        self.assertEqual(t.count("主页编辑："), 1)
        again = self.worker([edit])["e:1"]                        # 再跑一次：无变化，不重复记日志
        self.assertFalse(again["changed"])
        self.assertEqual(self.text(), t)

    def test_failures_leave_card_untouched(self):
        out = self.worker([
            {"id": "e:2", "target": "p:none", "fields": {"next": "x"}},
            {"id": "e:3", "target": "p:test", "fields": {"status": "bogus"}},
            {"id": "e:4", "target": "p:test", "milestones": [{"text": "不存在的里程碑", "done": True}]},
        ])
        self.assertEqual({k: v["state"] for k, v in out.items()}, {"e:2": "failed", "e:3": "failed", "e:4": "failed"})
        self.assertIn("找不到项目卡", out["e:2"]["error"])
        self.assertEqual(self.text(), CARD)


if __name__ == "__main__":
    unittest.main()
