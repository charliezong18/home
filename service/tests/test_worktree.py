"""H4：home-data 以 git worktree 部署时（.git 是文件），merge=union 仍要配上，/health 的 rebasing 要看得见。"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402
import common  # noqa: E402
import config  # noqa: E402
import dibao  # noqa: E402

sh = _env.sh


def git_id(repo):
    sh("git", "-C", repo, "config", "user.email", "t@example.com")
    sh("git", "-C", repo, "config", "user.name", "t")


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        self.remote = os.path.join(root, "remote.git")
        self.main = os.path.join(root, "review")
        self.wt = os.path.join(root, "review-home-data")
        self.other = os.path.join(root, "other")
        sh("git", "init", "-q", "--bare", "-b", "main", self.remote)
        sh("git", "clone", "-q", self.remote, self.main)
        git_id(self.main)
        sh("git", "-C", self.main, "commit", "-q", "--allow-empty", "-m", "main")
        sh("git", "-C", self.main, "push", "-q", "origin", "HEAD:main")
        sh("git", "-C", self.main, "checkout", "-q", "--orphan", "home-data")
        with open(os.path.join(self.main, "edits.jsonl"), "w") as f:
            f.write('{"id":"e:0"}\n')
        sh("git", "-C", self.main, "add", "edits.jsonl")
        sh("git", "-C", self.main, "commit", "-q", "-m", "init")
        sh("git", "-C", self.main, "push", "-q", "-u", "origin", "home-data")
        sh("git", "-C", self.main, "checkout", "-q", "main")
        sh("git", "-C", self.main, "worktree", "add", "-q", self.wt, "home-data")
        sh("git", "-C", self.wt, "branch", "-q", "--set-upstream-to", "origin/home-data")
        self.assertTrue(os.path.isfile(os.path.join(self.wt, ".git")))      # 真是 worktree：.git 是文件
        for mod in (common, dibao):
            p = mock.patch.object(mod, "DATA_DIR", self.wt)
            p.start()
            self.addCleanup(p.stop)
        self.svc = dibao.Service(config.load(), push=False)

    def test_prepare_repo_sets_union_in_worktree(self):
        self.svc.prepare_repo()
        attr = sh("git", "-C", self.wt, "check-attr", "merge", "--", "edits.jsonl", "home.json")
        self.assertIn("edits.jsonl: merge: union", attr)
        self.assertIn("home.json: merge: ours", attr)

    def test_concurrent_append_rebases_cleanly(self):
        self.svc.prepare_repo()
        sh("git", "clone", "-q", "-b", "home-data", self.remote, self.other)    # 「网页」那边先追加并推
        git_id(self.other)
        with open(os.path.join(self.other, "edits.jsonl"), "a") as f:
            f.write('{"id":"e:web"}\n')
        sh("git", "-C", self.other, "commit", "-qam", "web")
        sh("git", "-C", self.other, "push", "-q", "origin", "home-data")
        git_id(self.wt)
        with open(os.path.join(self.wt, "edits.jsonl"), "a") as f:            # dibao 同时本地追加
            f.write('{"id":"e:dibao"}\n')
        sh("git", "-C", self.wt, "commit", "-qam", "dibao")
        common.sync_down()                                                     # pull --rebase 不得停在冲突里
        with open(os.path.join(self.wt, "edits.jsonl")) as f:
            ids = [line.strip() for line in f if line.strip()]
        self.assertEqual(sorted(ids), sorted(['{"id":"e:0"}', '{"id":"e:web"}', '{"id":"e:dibao"}']))
        self.assertEqual(self.svc.worktree_state(), (False, False))

    def test_worktree_state_sees_rebase_in_progress(self):
        self.assertEqual(self.svc.worktree_state(), (False, False))
        rebase_dir = self.svc.git_path("rebase-merge")
        self.assertNotEqual(os.path.dirname(rebase_dir), os.path.join(self.wt, ".git"))
        os.makedirs(rebase_dir)                                                # 模拟停在 rebase 中途
        self.assertTrue(self.svc.worktree_state()[1])
        self.assertFalse(self.svc.health()["ok"])


if __name__ == "__main__":
    unittest.main()
