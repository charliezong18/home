"""apply_edits 整轮（cmd_apply / cmd_list / cmd_mark_done）：真 worker 子进程 + 真 git，remote = tmp 本地裸仓库。

覆盖：成功落卡并推送、幂等（第二轮不重放 / 不重复记日志）、与网页并发追加的 push 冲突重试、
push 连续失败回滚 + 下一轮重试、worker 超时 / 崩溃时账本不动、校验失败记 failed、工作树不干净拒绝。
vault 指向 tmp 里的 Dashboard，AI_HOME_NTFY_DRYRUN=1，DIBAO_CONFIG 指向不存在的文件 → 绝不外发通知。
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import _gitfixture as gf
import apply_edits
import common

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

## 日志
- 2026-10-01 建卡
"""


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jl(*objs):
    return "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objs)


def pending(eid, **kw):
    return {"id": eid, "ts": "2026-10-30T00:00:00Z", "target": "p:test", "state": "pending", "from": "web", **kw}


class ApplyEditsFlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.dash = os.path.join(self.tmp, "Dashboard")
        os.makedirs(self.dash)
        self.card = os.path.join(self.dash, "测试卡.md")
        with open(self.card, "w", encoding="utf-8") as f:
            f.write(CARD)
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(self.cache)
        self.remote = gf.make_remote(self.tmp)
        self.data = os.path.join(self.tmp, "data")
        # 子进程（worker / collect_dashboard / status.py）从环境变量拿路径，所以环境变量也指向 tmp
        env = mock.patch.dict(os.environ, {
            **gf.isolated_env(self.tmp), "AI_HOME_DASHBOARD": self.dash, "AI_HOME_DATA": self.data,
            "AI_HOME_CACHE": self.cache, "AI_HOME_NTFY_DRYRUN": "1",
            "DIBAO_CONFIG": os.path.join(self.tmp, "no-such-config.toml")})
        env.start()
        self.addCleanup(env.stop)
        gf.clone(self.remote, self.data)
        self.web = gf.clone(self.remote, os.path.join(self.tmp, "web"))
        for target, name, value in [
            (common, "DATA_DIR", self.data), (common, "CACHE_DIR", self.cache),
            (common, "LOCK_FILE", os.path.join(self.cache, "git.lock")),
            (apply_edits, "LEDGER", os.path.join(self.data, "edits.jsonl")),
        ]:
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.quiet = contextlib.redirect_stdout(io.StringIO())

    # ---------- 工具 ----------
    def web_append(self, *objs):
        gf.commit_file(self.web, "edits.jsonl", jl(*objs), "web: edit", append=True)

    def remote_ledger(self):
        return [json.loads(x) for x in (gf.remote_show(self.remote, "edits.jsonl") or "").splitlines() if x.strip()]

    def states(self):
        out = {}
        for e in self.remote_ledger():
            out[e["id"]] = e.get("state", out.get(e["id"]))
        return out

    def card_text(self):
        with open(self.card, encoding="utf-8") as f:
            return f.read()

    def run_apply(self):
        with self.quiet:
            return apply_edits.cmd_apply()

    def clean(self):
        return gf.sh("git", "-C", self.data, "status", "--porcelain") == ""

    # ---------- 成功 / 幂等 ----------
    def test_success_applies_card_pushes_state_and_recollects(self):
        self.web_append(pending("e:1", fields={"next": "新的下一步"}, milestones=[{"text": "第一步", "done": True}]))
        self.assertEqual(self.run_apply(), 0)
        t = self.card_text()
        self.assertIn("next: 新的下一步\n", t)
        self.assertIn("- [x] 第一步", t)
        self.assertEqual(t.count("主页编辑："), 1)
        self.assertEqual(self.states(), {"e:1": "applied"})
        last = self.remote_ledger()[-1]
        self.assertIn("next 改为「新的下一步」", last["summary"])
        self.assertEqual(gf.log_subjects(self.remote, gf.BRANCH)[0], "edits: apply 1")
        self.assertEqual(gf.rev(self.data), gf.rev(self.remote, gf.BRANCH))
        self.assertTrue(self.clean())
        # 卡改了 → 立刻重采 dashboard（快照写进 tmp 缓存）并记 status ok
        snap = load_json(os.path.join(self.cache, "snapshot-dashboard.json"))
        self.assertEqual(snap["data"]["cards"][0]["next"], "新的下一步")
        st = load_json(os.path.join(self.cache, "status.json"))
        self.assertEqual(st["dashboard"]["consecutive_failures"], 0)

    def test_second_round_is_idempotent(self):
        self.web_append(pending("e:1", fields={"next": "n1"}))
        self.assertEqual(self.run_apply(), 0)
        head, text = gf.rev(self.remote, gf.BRANCH), self.card_text()
        self.assertEqual(self.run_apply(), 0)                                # 没有 pending：不提交、不碰卡
        self.assertEqual(gf.rev(self.remote, gf.BRANCH), head)
        self.assertEqual(self.card_text(), text)
        self.assertEqual(len(self.remote_ledger()), 2)                       # 原始行 + 一条状态行

    def test_no_pending_no_commit(self):
        head = gf.rev(self.remote, gf.BRANCH)
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(gf.rev(self.remote, gf.BRANCH), head)

    def test_note_goes_to_hermes_then_list_and_mark_done(self):
        self.web_append(pending("e:n", fields={"next": "n"}, note="请 Hermes 看看"))
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.states(), {"e:n": "needs_hermes"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(apply_edits.cmd_list(), 0)
        self.assertEqual([e["id"] for e in json.loads(buf.getvalue())], ["e:n"])
        with self.quiet:
            self.assertEqual(apply_edits.cmd_mark_done("e:n", "处理了"), 0)
        self.assertEqual(self.states(), {"e:n": "done"})
        self.assertEqual(self.remote_ledger()[-1]["result"], "处理了")
        head = gf.rev(self.remote, gf.BRANCH)
        with self.quiet:
            self.assertEqual(apply_edits.cmd_mark_done("e:n", "又一次"), 0)    # 幂等：已 done 不重复记
        self.assertEqual(gf.rev(self.remote, gf.BRANCH), head)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(apply_edits.cmd_mark_done("e:none", ""), 1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            apply_edits.cmd_list()
        self.assertEqual(json.loads(buf.getvalue()), [])

    # ---------- 冲突 ----------
    def test_push_conflict_with_concurrent_web_append_retries(self):
        """网页在我们 sync 之后、push 之前又追加了一行 → push 被拒 → 撤回本地 commit、重拉、重追加，成功。"""
        self.web_append(pending("e:1", fields={"next": "n1"}))
        real_sync, calls = common.sync_down, []

        def racing_sync():
            real_sync()
            calls.append(1)
            if len(calls) == 2:                     # 第 1 次在 cmd_apply 开头；第 2 次在 append_lines 里
                self.web_append(pending("e:2", fields={"next": "n2"}))
        with mock.patch.object(apply_edits, "sync_down", racing_sync):
            self.assertEqual(self.run_apply(), 0)
        self.assertEqual(len(calls), 3)                                       # 重试了一次
        ids = [(e["id"], e["state"]) for e in self.remote_ledger()]
        self.assertEqual(ids, [("e:1", "pending"), ("e:2", "pending"), ("e:1", "applied")])   # 网页那行没丢、状态行只一条
        self.assertEqual(gf.rev(self.data), gf.rev(self.remote, gf.BRANCH))
        self.assertTrue(self.clean())
        self.assertEqual(self.run_apply(), 0)                                # 下一轮处理 e:2
        self.assertEqual(self.states(), {"e:1": "applied", "e:2": "applied"})
        self.assertIn("next: n2\n", self.card_text())

    # ---------- 失败重试 ----------
    def test_push_failing_three_times_rolls_back_then_next_round_succeeds(self):
        self.web_append(pending("e:1", fields={"next": "n1"}))
        before = gf.rev(self.remote, gf.BRANCH)
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho 'remote: 拒绝（测试）' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        with self.quiet, self.assertRaises(RuntimeError) as cm:
            apply_edits.cmd_apply()
        self.assertIn("3 次", str(cm.exception))
        self.assertEqual(gf.rev(self.data), before)                          # 本地 commit 已撤回
        self.assertTrue(self.clean())
        self.assertEqual(self.states(), {"e:1": "pending"})
        self.assertIn("next: n1\n", self.card_text())                       # 卡已落（worker 先跑），账本没推上
        os.remove(hook)                                                       # 远端恢复 → 下一轮重放
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.states(), {"e:1": "applied"})
        self.assertEqual(self.card_text().count("主页编辑："), 1)              # 重放不重复记日志
        self.assertEqual(self.remote_ledger()[-1]["summary"], "无实际变化")

    def _with_worker(self, fake):
        real_run = subprocess.run

        def run(cmd, *a, **kw):
            if isinstance(cmd, list) and "--worker" in cmd:
                return fake(cmd, *a, **kw)
            return real_run(cmd, *a, **kw)
        return mock.patch.object(subprocess, "run", run)

    def test_worker_timeout_skips_round_and_keeps_pending(self):
        self.web_append(pending("e:1", fields={"next": "n1"}))
        head = gf.rev(self.remote, gf.BRANCH)

        def boom(cmd, *a, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        with self._with_worker(boom):
            self.assertEqual(self.run_apply(), 2)
        self.assertEqual(gf.rev(self.remote, gf.BRANCH), head)
        self.assertEqual(self.states(), {"e:1": "pending"})
        self.assertEqual(self.card_text(), CARD)
        self.assertEqual(self.run_apply(), 0)                                # 下一轮正常 worker 补上
        self.assertEqual(self.states(), {"e:1": "applied"})

    def test_worker_crash_skips_round(self):
        self.web_append(pending("e:1", fields={"next": "n1"}))
        head = gf.rev(self.remote, gf.BRANCH)
        with self._with_worker(lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 1, "", "Traceback: boom")):
            self.assertEqual(self.run_apply(), 2)
        self.assertEqual(gf.rev(self.remote, gf.BRANCH), head)
        self.assertEqual(self.states(), {"e:1": "pending"})

    def test_validation_failure_recorded_and_card_untouched(self):
        self.web_append(pending("e:bad", fields={"status": "bogus"}), pending("e:nope", target="p:none", fields={"next": "x"}))
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.states(), {"e:bad": "failed", "e:nope": "failed"})
        errs = {e["id"]: e.get("error") for e in self.remote_ledger() if e["state"] == "failed"}
        self.assertIn("status=bogus", errs["e:bad"])
        self.assertIn("找不到项目卡", errs["e:nope"])
        self.assertEqual(self.card_text(), CARD)
        self.assertFalse(os.path.exists(os.path.join(self.cache, "snapshot-dashboard.json")))   # 没改卡就不重采

    def test_dirty_worktree_refuses_to_append(self):
        self.web_append(pending("e:1", fields={"next": "n1"}))
        head = gf.rev(self.remote, gf.BRANCH)
        with open(os.path.join(self.data, "stray.txt"), "w") as f:
            f.write("x")
        with self.quiet, self.assertRaises(RuntimeError) as cm:
            apply_edits.cmd_apply()
        self.assertIn("不干净", str(cm.exception))
        self.assertEqual(gf.rev(self.remote, gf.BRANCH), head)


if __name__ == "__main__":
    unittest.main()
