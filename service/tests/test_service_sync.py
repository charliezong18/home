"""dibao.Service 的 git 同步：sync_cycle（pull --rebase + 落卡 + 重算）/ _commit / do_push / /health 同步状态。

每个用例一套全新的 home-data 克隆 + 本地裸仓库 remote（tmp 里，离线），通过 mock 把 dibao / common / build_home
的路径指过去；共享的 _env.DATA 与快照用完还原，不影响其他测试。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402
import build_home  # noqa: E402
import common  # noqa: E402
import config  # noqa: E402
import dibao  # noqa: E402
import ledger  # noqa: E402
import life_ingest  # noqa: E402

sh = _env.sh
BR = "home-data"


def jl(*objs):
    return "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objs)


class ServiceSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="svcsync-", dir=_env.ROOT)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(_env.SNAP, encoding="utf-8") as f:
            snap = f.read()
        self.addCleanup(self._restore_snap, snap)
        self.remote = os.path.join(self.tmp, "remote.git")
        sh("git", "init", "-q", "--bare", "-b", BR, self.remote)
        self.data = os.path.join(self.tmp, "data")
        self.web = os.path.join(self.tmp, "web")
        seed = os.path.join(self.tmp, "seed")
        sh("git", "init", "-q", "-b", BR, seed)
        with open(os.path.join(seed, "README.md"), "w") as f:
            f.write("fake\n")
        sh("git", "-C", seed, "add", ".")
        sh("git", "-C", seed, "commit", "-qm", "init")
        sh("git", "-C", seed, "push", "-q", self.remote, f"HEAD:{BR}")
        for d in (self.data, self.web):
            sh("git", "clone", "-q", "-b", BR, self.remote, d)
        j = lambda name: os.path.join(self.data, name)  # noqa: E731
        for target, name, value in [
            (common, "DATA_DIR", self.data), (dibao, "DATA_DIR", self.data),
            (dibao, "EDITS", j("edits.jsonl")), (dibao, "READINGS", j("readings.jsonl")),
            (dibao, "HOME_JSON", j("home.json")), (dibao, "STATUS_JSON", j("status.json")),
            (dibao, "DEVICES", j("devices.jsonl")),
            (build_home, "DATA_DIR", self.data), (build_home, "LEDGER", j("readings.jsonl")),
            (life_ingest, "DATA_DIR", self.data),
        ]:
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        cfg = config.load()
        cfg.worker_timeout = 10
        self.svc = dibao.Service(cfg, push=False)
        self.svc.prepare_repo()

    # ---------- 工具 ----------
    @staticmethod
    def _restore_snap(text):
        with open(_env.SNAP, "w", encoding="utf-8") as f:
            f.write(text)

    def web_write(self, rel, text, msg="web"):
        sh("git", "-C", self.web, "pull", "-q", "--rebase", "origin", BR)
        with open(os.path.join(self.web, rel), "a", encoding="utf-8") as f:
            f.write(text)
        sh("git", "-C", self.web, "add", rel)
        sh("git", "-C", self.web, "commit", "-qm", msg)
        sh("git", "-C", self.web, "push", "-q", "origin", f"HEAD:{BR}")

    def local_reading(self, rid, title):
        with dibao.data_lock():
            ledger.append(dibao.READINGS, {"id": rid, "title": title, "url": f"https://e/{rid}", "status": "new",
                                           "added": "2026-10-30", "ts": "2026-10-30T00:00:00Z"})
            return self.svc._commit(["readings.jsonl"], f"local {rid}")

    def ahead(self):
        return int(sh("git", "-C", self.data, "rev-list", "--count", f"origin/{BR}..HEAD").strip() or 0)

    def remote_file(self, rel):
        return sh("git", "-C", self.remote, "show", f"{BR}:{rel}")

    # ---------- 用例 ----------
    def test_sync_cycle_applies_remote_pending_and_push_publishes_state(self):
        self.web_write("edits.jsonl", jl({"id": "e:w", "ts": "2026-10-30T00:00:00Z", "target": "p:test",
                                          "fields": {"next": "网页改"}, "state": "pending", "from": "web"}))
        self.svc.sync_cycle()
        self.assertEqual(ledger.read_ledger(dibao.EDITS)["e:w"]["state"], "applied")
        with open(dibao.HOME_JSON, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["projects"][0]["next"], "网页改")
        self.assertGreater(self.ahead(), 0)
        self.assertTrue(self.svc.do_push())
        self.assertEqual(self.ahead(), 0)
        self.assertIn('"state": "applied"', self.remote_file("edits.jsonl"))
        h = self.svc.health()
        self.assertEqual((h["ok"], h["last_sync_ok"], h["last_push_ok"], h["worktree_dirty"]), (True, True, True, False))

    def test_commit_without_change_returns_false_and_does_not_mark_pusher(self):
        self.svc.push = True
        with dibao.data_lock():
            self.assertFalse(self.svc._commit(["README.md"], "noop"))
        self.assertIsNone(self.svc.pusher.first_dirty)
        self.assertTrue(self.local_reading("link-a", "A"))
        self.assertIsNotNone(self.svc.pusher.first_dirty)                       # 有提交 → 进入 push 防抖

    def test_push_rejected_when_remote_ahead_then_sync_recovers(self):
        self.local_reading("link-a", "本地")
        self.web_write("notes.txt", "别处\n")
        self.assertFalse(self.svc.do_push())                                    # non-fast-forward
        h = self.svc.health()
        self.assertEqual((h["ok"], h["last_push_ok"]), (False, False))
        self.assertTrue(h["last_push_error"])
        self.svc.sync_cycle()                                                   # pull --rebase 把本地提交摞到远端之上
        self.assertTrue(self.svc.do_push())
        self.assertIn("本地", self.remote_file("readings.jsonl"))
        self.assertEqual(self.remote_file("notes.txt"), "别处\n")
        self.assertTrue(self.svc.health()["ok"])

    def test_concurrent_ledger_appends_union_merge_without_conflict(self):
        self.local_reading("link-l", "本地")
        self.web_write("readings.jsonl", jl({"id": "link-w", "title": "远端", "url": "https://e/w", "status": "new",
                                             "added": "2026-10-30", "ts": "t"}))
        self.svc.sync_cycle()
        h = self.svc.health()
        self.assertEqual((h["last_sync_ok"], h["rebasing"], h["worktree_dirty"]), (True, False, False))
        self.assertEqual(set(ledger.read_ledger(dibao.READINGS)), {"link-l", "link-w"})

    def test_generated_files_conflict_resolved_without_stuck_rebase(self):
        """home.json / status.json 两边都改：merge=ours，sync 不卡在 rebase 中，随后重算覆盖。"""
        with dibao.data_lock():
            with open(dibao.HOME_JSON, "w") as f:
                f.write('{"who": "local"}\n')
            self.svc._commit(["home.json"], "local home")
        self.web_write("home.json", '{"who": "remote"}\n')
        self.svc.sync_cycle()
        h = self.svc.health()
        self.assertEqual((h["last_sync_ok"], h["rebasing"], h["worktree_dirty"]), (True, False, False))

    def test_remote_without_branch_is_ok(self):
        empty = os.path.join(self.tmp, "empty.git")
        sh("git", "init", "-q", "--bare", "-b", BR, empty)
        sh("git", "-C", self.data, "remote", "set-url", "origin", empty)
        self.svc.sync_cycle()
        self.assertTrue(self.svc.health()["last_sync_ok"])

    def test_unreachable_remote_reported_as_sync_failure(self):
        """远端不可达（断网 / 仓库地址失效）：/health 必须报 sync 失败，而不是当成「远端还没有该分支」静默跳过。"""
        sh("git", "-C", self.data, "remote", "set-url", "origin", os.path.join(self.tmp, "gone.git"))
        self.svc.sync_cycle()
        h = self.svc.health()
        self.assertFalse(h["last_sync_ok"])
        self.assertFalse(h["ok"])
        self.assertTrue(h["last_sync_error"])
        sh("git", "-C", self.data, "remote", "set-url", "origin", self.remote)  # 恢复 → 自愈
        self.svc.sync_cycle()
        self.assertTrue(self.svc.health()["ok"])


if __name__ == "__main__":
    unittest.main()
