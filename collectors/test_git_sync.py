"""common.py 的 git 同步封装：git / sync_down / commit_and_push / data_lock。

remote = tmp 里的本地裸仓库，另一个克隆模拟网页 / iOS 写入；不访问网络、不读全局 git 配置。
"""
import os
import tempfile
import threading
import unittest
from unittest import mock

import _gitfixture as gf
import common


class GitSyncTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        env = mock.patch.dict(os.environ, gf.isolated_env(self.tmp))
        env.start()
        self.addCleanup(env.stop)
        self.remote = gf.make_remote(self.tmp)
        self.data = gf.clone(self.remote, os.path.join(self.tmp, "data"))
        self.web = gf.clone(self.remote, os.path.join(self.tmp, "web"))
        cache = os.path.join(self.tmp, "cache")
        for name, value in (("DATA_DIR", self.data), ("CACHE_DIR", cache), ("LOCK_FILE", os.path.join(cache, "git.lock"))):
            p = mock.patch.object(common, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write(self, rel, text, mode="w"):
        with open(os.path.join(self.data, rel), mode, encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.data, rel), encoding="utf-8") as f:
            return f.read()

    # ---------- git() ----------
    def test_git_runs_in_data_dir_and_check_raises(self):
        self.assertEqual(common.git("rev-parse", "--show-toplevel").stdout.strip(), os.path.realpath(self.data))
        r = common.git("rev-parse", "no-such-ref", check=False)
        self.assertNotEqual(r.returncode, 0)
        with self.assertRaises(RuntimeError) as cm:
            common.git("rev-parse", "--verify", "no-such-ref")
        self.assertIn("rc=", str(cm.exception))

    # ---------- sync_down ----------
    def test_sync_down_pulls_remote_commits(self):
        gf.commit_file(self.web, "edits.jsonl", '{"id":"e:1","state":"pending"}\n', "web: edit")
        common.sync_down()
        self.assertEqual(self.read("edits.jsonl"), '{"id":"e:1","state":"pending"}\n')
        self.assertEqual(gf.rev(self.data), gf.rev(self.remote, gf.BRANCH))

    def test_sync_down_rebases_local_unpushed_commit(self):
        self.write("local.txt", "本地\n")
        gf.sh("git", "-C", self.data, "add", "local.txt")
        gf.sh("git", "-C", self.data, "commit", "-qm", "local")
        gf.commit_file(self.web, "remote.txt", "远端\n", "web")
        common.sync_down()
        self.assertEqual(gf.log_subjects(self.data)[:3], ["local", "web", "init"])     # 线性，本地提交在上
        self.assertEqual(self.read("remote.txt"), "远端\n")
        self.assertEqual(gf.sh("git", "-C", self.data, "rev-list", "--merges", "HEAD"), "")

    def test_sync_down_skips_when_remote_has_no_branch(self):
        empty = os.path.join(self.tmp, "empty.git")
        gf.sh("git", "init", "-q", "--bare", "-b", gf.BRANCH, empty)
        gf.sh("git", "-C", self.data, "remote", "set-url", "origin", empty)
        head = gf.rev(self.data)
        common.sync_down()                                                   # 不抛
        self.assertEqual(gf.rev(self.data), head)

    def test_sync_down_unreachable_remote_raises(self):
        # 远端不可达 ≠ 远端没有该分支：必须抛出，dibao /health 才能报 sync 失败
        gf.sh("git", "-C", self.data, "remote", "set-url", "origin", os.path.join(self.tmp, "missing.git"))
        head = gf.rev(self.data)
        with self.assertRaises(RuntimeError) as cm:
            common.sync_down()
        self.assertIn("ls-remote", str(cm.exception))
        self.assertEqual(gf.rev(self.data), head)

    def test_sync_down_conflict_raises(self):
        self.write("README.md", "本地改\n")
        gf.sh("git", "-C", self.data, "commit", "-qam", "local")
        gf.commit_file(self.web, "README.md", "远端改\n", "web")
        with self.assertRaises(RuntimeError) as cm:
            common.sync_down()
        self.assertIn("pull --rebase", str(cm.exception))

    # ---------- commit_and_push ----------
    def test_commit_and_push_no_change_returns_false(self):
        head = gf.rev(self.data)
        self.assertFalse(common.commit_and_push(["README.md"], "noop"))
        self.assertEqual(gf.rev(self.data), head)

    def test_commit_and_push_only_given_paths(self):
        self.write("home.json", "{}\n")
        self.write("stray.txt", "别提交我\n")
        self.assertTrue(common.commit_and_push(["home.json"], "home: update"))
        self.assertEqual(gf.remote_show(self.remote, "home.json"), "{}\n")
        self.assertIsNone(gf.remote_show(self.remote, "stray.txt"))
        self.assertEqual(gf.log_subjects(self.remote, gf.BRANCH)[0], "home: update")
        self.assertIn("?? stray.txt", gf.sh("git", "-C", self.data, "status", "--porcelain"))

    def test_commit_and_push_rejected_when_remote_ahead(self):
        gf.commit_file(self.web, "x.txt", "x\n", "web")
        self.write("home.json", "{}\n")
        with self.assertRaises(RuntimeError) as cm:
            common.commit_and_push(["home.json"], "home: update")
        self.assertIn("push", str(cm.exception))
        self.assertEqual(gf.log_subjects(self.data)[0], "home: update")            # 本地提交还在，下一轮 sync_down 再 rebase
        common.sync_down()
        gf.sh("git", "-C", self.data, "push", "-q", "origin", f"HEAD:{gf.BRANCH}")
        self.assertEqual(gf.log_subjects(self.remote, gf.BRANCH)[:2], ["home: update", "web"])

    # ---------- data_lock ----------
    def test_data_lock_is_exclusive(self):
        held, release, order = threading.Event(), threading.Event(), []

        def holder():
            with common.data_lock():
                held.set()
                release.wait(5)
                order.append("holder-out")

        def waiter():
            with common.data_lock():
                order.append("waiter-in")

        t1 = threading.Thread(target=holder)
        t1.start()
        self.assertTrue(held.wait(5))
        t2 = threading.Thread(target=waiter)
        t2.start()
        t2.join(0.3)
        self.assertTrue(t2.is_alive(), "锁被持有时第二个 data_lock 不该进得去")
        release.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual(order, ["holder-out", "waiter-in"])
        self.assertTrue(os.path.exists(common.LOCK_FILE))


if __name__ == "__main__":
    unittest.main()
