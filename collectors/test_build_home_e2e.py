"""build_home.main() 端到端：真实 sync_down / commit_and_push / data_lock，remote = tmp 里的本地裸仓库。

给定临时缓存目录里的快照 + status.json，以及远端（模拟网页/iOS/cron 写入）的 readings / edits / life 账本，
断言 home.json / status.json 的产物、提交信息、「内容不变不提交」、心跳，以及远端领先时的 rebase。
完全离线，不碰真实 home-data / vault / ~/Library/Caches，不发通知。
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

import _gitfixture as gf
import build_home
import common
import life_ingest


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def snap(source, data, fetched="2026-10-30T08:00:00Z"):
    return {"source": source, "fetched": fetched, "data": data}


class BuildHomeE2ETest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        env = mock.patch.dict(os.environ, {**gf.isolated_env(self.tmp), "AI_HOME_NTFY_DRYRUN": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.remote = gf.make_remote(self.tmp)
        self.data = gf.clone(self.remote, os.path.join(self.tmp, "data"))
        self.web = gf.clone(self.remote, os.path.join(self.tmp, "web"))      # 另一个写者（网页 / iOS / cron）
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(self.cache)
        for target, name, value in [
            (common, "DATA_DIR", self.data), (common, "CACHE_DIR", self.cache),
            (common, "LOCK_FILE", os.path.join(self.cache, "git.lock")),
            (build_home, "DATA_DIR", self.data), (build_home, "CACHE_DIR", self.cache),
            (build_home, "LEDGER", os.path.join(self.data, "readings.jsonl")),
            (life_ingest, "DATA_DIR", self.data),
        ]:
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.today = dt.date.today()

    # ---------- 夹具 ----------
    def d(self, days):
        return (self.today + dt.timedelta(days=days)).isoformat()

    def write_cache(self, name, obj):
        with open(os.path.join(self.cache, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def full_fixture(self):
        self.write_cache("snapshot-menxia.json", snap("menxia", {
            "total": 3, "counts": {"你拍": 1, "你读": 1, "agent": 0, "闲": 1, "未标注": 0},
            "oldest": {"你拍": [{"n": 179, "title": "折 179", "created": "2026-10-01"}]},
            "library": [{"n": 180, "title": "读物 180", "kind": "读物", "created": "2026-10-02"}]}))
        self.write_cache("snapshot-dashboard.json", snap("dashboard", {"cards": [
            {"id": "p:a", "title": "卡 A", "next": "a1"}, {"id": "p:b", "title": "卡 B", "next": "b1"}]},
            fetched="2026-10-30T09:00:00Z"))
        nxt = self.today + dt.timedelta(days=5)
        self.write_cache("snapshot-countdown.json", snap("countdown", {"items": [
            {"label": "示例 生日", "year": None, "month": nxt.month, "day": nxt.day, "recur": True},
            {"label": "很久以前", "year": 2001, "month": 1, "day": 1, "recur": False}]}))   # 窗口外，滤掉
        self.write_cache("snapshot-backlog.json", snap("backlog", {"url": "obsidian://backlog", "items": [
            {"title": "报税", "date": self.d(10), "note": "11-30 前"}, {"title": "无日期事项"}]}))
        self.write_cache("status.json", {
            "menxia": {"consecutive_failures": 2, "last_error_msg": "gh 超时", "last_ok": "2026-10-29T00:00:00Z"},
            "dashboard": {"consecutive_failures": 0, "last_error_msg": "旧错误", "last_ok": "2026-10-30T09:00:00Z"}})
        # 远端账本：读物两条（一条后来 done）、编辑三条（pending / needs_hermes / applied）、life 一条
        gf.commit_file(self.web, "readings.jsonl", "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [
            {"id": "link-20261029-aaaa", "title": "A", "url": "https://e/a", "added": self.d(-1), "ts": "2026-10-29T01:00:00Z", "status": "new"},
            {"id": "link-20261029-bbbb", "title": "B", "url": "https://e/b", "added": self.d(-1), "ts": "2026-10-29T02:00:00Z", "status": "new"},
            {"id": "link-20261029-bbbb", "status": "done", "ts": "2026-10-29T03:00:00Z", "from": "web"}]), "web: readings")
        gf.commit_file(self.web, "edits.jsonl", "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [
            {"id": "e:1", "target": "p:a", "state": "pending", "fields": {"next": "x"}},
            {"id": "e:2", "target": "p:a", "state": "pending", "note": "看看"},
            {"id": "e:2", "state": "needs_hermes"},
            {"id": "e:3", "target": "p:b", "state": "applied"},
            "not json"]).replace('"not json"', "{坏行"), "web: edits")
        gf.commit_file(self.web, "life.jsonl", json.dumps(
            {"id": "d:cal-11112222", "title": "示例会议", "date": self.d(2), "source": "calendar",
             "status": "open", "ts": "2026-10-30T07:00:00Z"}, ensure_ascii=False) + "\n", "cron: life")

    def home(self, where="local"):
        if where == "remote":
            return json.loads(gf.remote_show(self.remote, "home.json"))
        with open(os.path.join(self.data, "home.json"), encoding="utf-8") as f:
            return json.load(f)

    # ---------- 用例 ----------
    def test_full_build_pulls_ledgers_and_pushes_artifacts(self):
        self.full_fixture()
        self.assertEqual(build_home.main(), 0)
        home = self.home("remote")                                     # 已 push 到远端
        self.assertEqual(home, self.home("local"))
        self.assertEqual(gf.rev(self.data), gf.rev(self.remote, gf.BRANCH))
        self.assertEqual(gf.log_subjects(self.remote, gf.BRANCH)[0], "home: update")
        self.assertEqual(gf.sh("git", "-C", self.data, "status", "--porcelain"), "")

        self.assertEqual(home["schema"], build_home.SCHEMA)
        self.assertTrue(home["generated"].endswith("Z"))
        self.assertEqual(home["folders"]["total"], 3)
        self.assertEqual(home["folders"]["counts"]["你拍"], 1)
        self.assertEqual(home["folders"]["oldest"]["你拍"][0]["n"], 179)
        self.assertEqual([x["n"] for x in home["library"]], [180])
        # 项目卡带编辑计数：p:a 一条 pending + 一条 needs_hermes；p:b 已 applied 不计
        cards = {p["id"]: p for p in home["projects"]}
        self.assertEqual((cards["p:a"]["pending_edits"], cards["p:a"]["hermes_edits"]), (1, 1))
        self.assertEqual((cards["p:b"]["pending_edits"], cards["p:b"]["hermes_edits"]), (0, 0))
        # 读物：远端账本经 sync_down 拉下来；done 的不展示
        self.assertEqual([r["rid"] for r in home["readings"]], ["r:20261029-aaaa"])
        self.assertFalse(home["readings"][0]["folded"])
        # 到期：倒数日 + backlog（有日期 / 无日期排最后）+ life，窗口外的滤掉
        self.assertEqual([x["id"] for x in home["due"]],
                         ["d:cal-11112222", "d:countdown-示例-生日", "d:backlog-报税", "d:backlog-无日期事项"])
        self.assertEqual(home["due"][2]["url"], "obsidian://backlog")
        # sources：每个源都有一项；失败源带错误，成功源清掉旧错误
        src = home["sources"]
        self.assertEqual(set(src), set(common.SOURCES))
        self.assertEqual((src["menxia"]["ok"], src["menxia"]["consecutive_failures"], src["menxia"]["last_error"]),
                         (False, 2, "gh 超时"))
        self.assertEqual((src["dashboard"]["ok"], src["dashboard"]["last_error"], src["dashboard"]["updated"]),
                         (True, "", "2026-10-30T09:00:00Z"))
        self.assertTrue(src["readings"]["ok"])
        self.assertEqual(src["life"]["updated"], "2026-10-30T07:00:00Z")
        # status.json = 缓存里的权威副本原样抄过去
        with open(os.path.join(self.cache, "status.json"), encoding="utf-8") as f:
            self.assertEqual(json.loads(gf.remote_show(self.remote, "status.json")), json.load(f))

    def test_unchanged_content_does_not_commit_then_change_does(self):
        self.full_fixture()
        self.assertEqual(build_home.main(), 0)
        head = gf.rev(self.data)
        self.assertEqual(build_home.main(), 0)                          # generated / updated 变了但内容没变
        self.assertEqual(gf.rev(self.data), head)
        self.assertEqual(gf.rev(self.remote, gf.BRANCH), head)
        # status 里只有 last_ok 变化也不算变化
        st = load_json(os.path.join(self.cache, "status.json"))
        st["dashboard"]["last_ok"] = "2026-10-31T00:00:00Z"
        self.write_cache("status.json", st)
        self.assertEqual(build_home.main(), 0)
        self.assertEqual(gf.rev(self.data), head)
        # 真变化：卡的 next 改了 → 新提交
        s = load_json(os.path.join(self.cache, "snapshot-dashboard.json"))
        s["data"]["cards"][0]["next"] = "a2"
        self.write_cache("snapshot-dashboard.json", s)
        self.assertEqual(build_home.main(), 0)
        self.assertNotEqual(gf.rev(self.data), head)
        self.assertEqual(gf.log_subjects(self.remote, gf.BRANCH)[0], "home: update")
        self.assertEqual(self.home("remote")["projects"][0]["next"], "a2")

    def test_heartbeat_when_committed_home_is_stale(self):
        self.full_fixture()
        self.assertEqual(build_home.main(), 0)
        h = self.home("local")
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=build_home.HEARTBEAT_H + 1)
        h["generated"] = old.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with open(os.path.join(self.data, "home.json"), "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False)
        gf.sh("git", "-C", self.data, "commit", "-qam", "test: age home.json")
        gf.sh("git", "-C", self.data, "push", "-q", "origin", f"HEAD:{gf.BRANCH}")
        self.assertEqual(build_home.main(), 0)
        self.assertEqual(gf.log_subjects(self.remote, gf.BRANCH)[0], "home: heartbeat")
        self.assertGreater(self.home("remote")["generated"], h["generated"])

    def test_remote_ahead_is_rebased_before_push(self):
        self.full_fixture()
        self.assertEqual(build_home.main(), 0)
        gf.commit_file(self.web, "notes.txt", "别人改的\n", "web: unrelated")   # 远端领先一个无关提交
        s = load_json(os.path.join(self.cache, "snapshot-dashboard.json"))
        s["data"]["cards"][1]["next"] = "b2"
        self.write_cache("snapshot-dashboard.json", s)
        self.assertEqual(build_home.main(), 0)
        subjects = gf.log_subjects(self.remote, gf.BRANCH)
        self.assertEqual(subjects[:2], ["home: update", "web: unrelated"])        # 线性：rebase 而非 merge
        self.assertEqual(gf.remote_show(self.remote, "notes.txt"), "别人改的\n")
        self.assertEqual(gf.rev(self.data), gf.rev(self.remote, gf.BRANCH))

    def test_empty_cache_still_builds_skeleton(self):
        self.assertEqual(build_home.main(), 0)                          # 没有任何快照 / status / 账本
        home = self.home("remote")
        self.assertEqual((home["folders"]["total"], home["projects"], home["readings"], home["due"], home["library"]),
                         (0, [], [], [], []))
        for s in ("menxia", "dashboard", "countdown", "backlog"):
            self.assertIsNone(home["sources"][s]["updated"], s)
            self.assertTrue(home["sources"][s]["ok"], s)
        self.assertIsNone(home["sources"]["life"]["updated"])
        self.assertEqual(json.loads(gf.remote_show(self.remote, "status.json")), {})

    def test_bad_reading_line_returns_3_but_still_pushes(self):
        gf.commit_file(self.web, "readings.jsonl", '{"id": "link-x", "title": "X", "added": "2026-10-30"}\n{截断\n', "web")
        self.assertEqual(build_home.main(), 3)
        home = self.home("remote")
        self.assertEqual([r["rid"] for r in home["readings"]], ["r:x"])
        self.assertFalse(home["sources"]["readings"]["ok"])
        self.assertIn("1 行无效", home["sources"]["readings"]["last_error"])


if __name__ == "__main__":
    unittest.main()
