"""approvals.py 单测。全部 FIXTURE：假 gh、临时状态目录、临时 Hermes db；不碰网络与真台账。
跑：python3 -m unittest collectors/test_approvals.py -v（在 ~/Developer/home 下）
"""
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approvals as A  # noqa: E402

R = A.REPO


def pr(n, merged_at, kind="设计", sha=None, updated=None, ref=None, body="## 待你拍板\n\n1) 甲？2) 乙？\n"):
    return {"number": n, "title": f"FIXTURE #{n}", "merged_at": merged_at, "updated_at": updated or merged_at or "2026-10-24T00:00:00Z",
            "merge_commit_sha": sha or f"{n:040d}", "labels": [{"name": f"kind:{kind}"}, {"name": "wait:你拍"}],
            "head": {"ref": ref or f"branch-{n}"}, "merged_by": {"login": "fixture"}, "body": body}


class FakeGH:
    def __init__(self, prs, comments=None):
        self.prs, self.comments, self.fail, self.calls = prs, comments or {}, False, []
        self.labels = {}
        self.repo_labels = {"wait:你拍", "wait:你读", "kind:设计", "wait:agent"}  # 缺 agent:claimed / agent:blocked
        self.created = []

    def __call__(self, path):
        self.calls.append(path)
        if self.fail:
            raise RuntimeError("FIXTURE gh down")
        if "pulls?state=closed" in path:
            page = int(path.rsplit("page=", 1)[1])
            items = sorted(self.prs, key=lambda p: p["updated_at"], reverse=True)
            return items[(page - 1) * 50: page * 50]
        page = int(path.rsplit("page=", 1)[1]) if "page=" in path else 1
        if path.startswith(f"repos/{R}/labels?"):
            return [{"name": l} for l in sorted(self.repo_labels)][(page - 1) * 100: page * 100]
        if "/issues/" in path and "/comments" in path:
            items = self.comments.get(int(path.split("/issues/")[1].split("/")[0]), [])
            return items[(page - 1) * 100: page * 100]
        if "/comments" in path:
            return []
        n = int(path.rsplit("/", 1)[1])
        return {"labels": [{"name": l} for l in sorted(self.labels.get(n, {"wait:你拍", "kind:设计"}))]}

    def edit(self, cmd):
        if cmd[1] == "label":  # gh label create NAME
            self.repo_labels.add(cmd[3])
            self.created.append(cmd[3])
            return
        n = int(cmd[3])
        cur = set(self.labels.get(n, {"wait:你拍", "kind:设计"}))
        for i, c in enumerate(cmd):
            if c == "--add-label":
                if cmd[i + 1] not in self.repo_labels:  # 真 gh：label 不存在就失败
                    raise subprocess.CalledProcessError(1, cmd, stderr=f"'{cmd[i + 1]}' not found")
                cur.add(cmd[i + 1])
            if c == "--remove-label":
                cur.discard(cmd[i + 1])
        self.labels[n] = cur


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        A.STATE_DIR = os.path.join(self.tmp.name, "state")
        self.db = os.path.join(self.tmp.name, "state.db")
        con = sqlite3.connect(self.db)
        con.execute("create table sessions (id text primary key, source text)")
        con.execute("create table messages (id integer primary key, session_id text, content text, tool_calls text, timestamp real)")
        con.executemany("insert into sessions values (?,?)",
                        [("owner-s", "desktop"), ("tele-s", "telegram"), ("cron-s", "cron"), ("tie-a", "desktop"), ("tie-b", "desktop")])
        t = lambda s: dt.datetime.fromisoformat(s).timestamp()
        con.executemany("insert into messages (session_id, content, tool_calls, timestamp) values (?,?,?,?)", [
            ("owner-s", "push branch-190 then open PR", None, t("2026-10-23T16:50:00+00:00")),
            ("owner-s", None, '{"cmd":"gh pr create --head branch-190"}', t("2026-10-23T16:52:00+00:00")),
            ("cron-s", "branch-190 nightly card", None, t("2026-10-24T06:00:00+00:00")),
            ("cron-s", "branch-190 again", None, t("2026-10-24T06:01:00+00:00")),
            ("cron-s", "branch-190 again2", None, t("2026-10-24T06:02:00+00:00")),
            ("tele-s", "branch-190 after merge", None, t("2026-10-24T20:23:00+00:00")),
            ("tele-s", "branch-190 after merge 2", None, t("2026-10-24T20:24:00+00:00")),
            ("tele-s", "branch-190 after merge 3", None, t("2026-10-24T20:25:00+00:00")),
            ("tie-a", "branch-300", None, t("2026-10-20T00:00:00+00:00")),
            ("tie-b", "branch-300", None, t("2026-10-20T00:00:00+00:00")),
        ])
        con.commit()
        con.close()
        A.HERMES_DB = self.db
        self.clock = dt.datetime(2026, 10, 24, 20, 30, tzinfo=dt.timezone.utc)
        A.now = lambda: self.clock

    def tearDown(self):
        self.tmp.cleanup()

    def jobs(self):
        with A.ledger() as d:
            return json.loads(json.dumps(d))


class TestScan(Base):
    def test_merge_creates_pending_job_with_owner_and_decisions(self):
        gh = FakeGH([pr(190, "2026-10-24T20:21:55Z")], {190: [
            {"id": 1, "created_at": "2026-10-24T20:20:32Z", "body": "<!-- menxia-readstate {} -->\n_续读_"},
            {"id": 2, "created_at": "2026-10-24T20:21:15Z", "body": "1. 按推荐：agent自己做"},
            {"id": 3, "created_at": "2026-10-24T20:21:30Z", "body": "**回话**\n收到"}]})
        res = A.scan(gh=gh)
        self.assertEqual(res["new-job"], [190])
        j = self.jobs()["jobs"][f"{R}#190@{190:040d}"]
        self.assertEqual(j["state"], "pending")
        self.assertEqual(j["state_zh"], "待接手")
        self.assertFalse(j["merge_is_authorization"])
        self.assertIsNone(j["authorized_scope"])
        self.assertEqual([d["body"] for d in j["decisions"]], ["1. 按推荐：agent自己做"])
        self.assertEqual(j["owner_session"], "owner-s")  # cron 与 merge 后的会话都不算
        self.assertIn("1) 甲？", j["pending_decisions"])

    def test_duplicate_event_is_noop(self):
        gh = FakeGH([pr(190, "2026-10-24T20:21:55Z")])
        A.scan(gh=gh)
        self.clock += dt.timedelta(minutes=5)
        res = A.scan(gh=gh)
        self.assertEqual(res["dup"], [190])
        self.assertEqual(len(self.jobs()["jobs"]), 1)

    def test_same_pr_new_merge_sha_is_distinct_key(self):
        A.scan(gh=FakeGH([pr(190, "2026-10-24T20:21:55Z")]))
        self.clock += dt.timedelta(minutes=5)
        A.scan(gh=FakeGH([pr(190, "2026-10-24T20:21:55Z", sha="f" * 40)]))
        self.assertEqual(len(self.jobs()["jobs"]), 2)

    def test_reading_and_reference_are_not_jobs(self):
        gh = FakeGH([pr(191, "2026-10-24T20:00:00Z", kind="读物"), pr(176, "2026-10-24T19:00:00Z", kind="参考"),
                     pr(180, None, updated="2026-10-24T19:30:00Z")])  # 180: 关而未合
        res = A.scan(gh=gh)
        d = self.jobs()
        self.assertEqual(d["jobs"], {})
        self.assertEqual(sorted(res["new-reference"]), [176, 191])
        self.assertEqual(len(d["references"]), 2)

    def test_folded_into_parent_is_reference_not_job(self):
        # 合并速审：原折留「已并入 #N…决定以 #N 为准」后被 merge，执行归父折那一单，不另生成待接手单
        gh = FakeGH([pr(192, "2026-10-24T20:10:00Z", kind="拍板"), pr(186, "2026-10-24T20:11:00Z")], {
            186: [{"id": 9, "created_at": "2026-10-24T20:10:58Z",
                   "body": "已并入 #192（2026-10-28 画可），决定以 #192 为准：https://x/?pr=192"}],
            192: [{"id": 8, "created_at": "2026-10-24T20:09:00Z", "body": "原 4 折 #186 已并入本折"}]})
        res = A.scan(gh=gh)
        d = self.jobs()
        self.assertEqual(res["new-job"], [192])  # 父折自己提「已并入本折」不算被并入
        self.assertEqual(res["new-reference"], [186])
        ref = d["references"][f"{R}#186@{186:040d}"]
        self.assertEqual(ref["merged_into"], 192)
        self.assertIn("#192", ref["note"])

    def test_folded_marker_from_inline_or_self_is_ignored(self):
        gh = FakeGH([pr(187, "2026-10-24T20:11:00Z")], {
            187: [{"id": 7, "created_at": "2026-10-24T20:10:00Z", "body": "> 引文\n\n已并入 #187 自己"}]})
        res = A.scan(gh=gh)
        self.assertEqual(res["new-job"], [187])

    def test_restart_and_failure_catch_up(self):
        gh = FakeGH([])
        A.scan(gh=gh)                       # 20:30 游标
        cursor1 = self.jobs()["cursor"]
        self.clock += dt.timedelta(hours=3)  # 停机 3 小时，期间合了 #201
        gh.prs = [pr(201, "2026-10-24T21:10:00Z")]
        gh.fail = True
        with self.assertRaises(RuntimeError):
            A.scan(gh=gh)
        self.assertEqual(self.jobs()["cursor"], cursor1)  # 失败不前移
        gh.fail = False
        res = A.scan(gh=gh)
        self.assertEqual(res["new-job"], [201])

    def test_late_visible_merge_within_overlap(self):
        A.scan(gh=FakeGH([]))  # 游标 20:30
        self.clock += dt.timedelta(minutes=5)
        # merged_at 早于游标（API 延迟可见），仍在 OVERLAP 内 → 补到
        res = A.scan(gh=FakeGH([pr(202, "2026-10-24T20:29:00Z")]))
        self.assertEqual(res["new-job"], [202])

    def test_owner_tie_is_unresolved(self):
        A.scan(gh=FakeGH([pr(300, "2026-10-24T20:00:00Z")]))
        j = next(iter(self.jobs()["jobs"].values()))
        self.assertIsNone(j["owner_session"])
        self.assertIn("并列", j["owner_reason"])


class TestTransitions(Base):
    def setUp(self):
        super().setUp()
        self.gh = FakeGH([pr(190, "2026-10-24T20:21:55Z")])
        A.scan(gh=self.gh)
        self.key = f"{R}#190@{190:040d}"

    def test_merge_is_not_authorization(self):
        with self.assertRaises(SystemExit):
            A.transition(self.key, "executing")
        j, _ = A.transition(self.key, "executing", authorized_scope="仅期0 PWA", task_id="t1", owner_session="owner-s")
        self.assertEqual(j["state_zh"], "执行中")

    def test_done_needs_verifying_and_evidence_and_is_terminal(self):
        A.transition(self.key, "executing", authorized_scope="仅期0 PWA")
        with self.assertRaises(SystemExit):
            A.transition(self.key, "done", evidence="x")
        A.transition(self.key, "verifying")
        with self.assertRaises(SystemExit):
            A.transition(self.key, "done")
        A.transition(self.key, "done", evidence="读回 OK")
        with self.assertRaises(SystemExit):
            A.transition(self.key, "executing")

    def test_event_id_idempotent(self):
        A.transition(self.key, "blocked", reason="缺签名授权", event_id="ev1")
        j, st = A.transition(self.key, "blocked", reason="缺签名授权", event_id="ev1")
        self.assertEqual(st, "dup-event")
        self.assertEqual(sum(1 for h in j["history"] if h["to"] == "blocked"), 1)

    def test_sync_labels_per_state(self):
        r = A.sync(self.key, gh=self.gh, edit=self.gh.edit)
        self.assertEqual(r["labels_readback"], ["kind:设计", "wait:agent"])
        self.assertTrue(r["verified"])
        A.transition(self.key, "executing", authorized_scope="s")
        self.assertIn("agent:claimed", A.sync(self.key, gh=self.gh, edit=self.gh.edit)["labels_readback"])
        A.transition(self.key, "blocked", reason="r")
        r = A.sync(self.key, gh=self.gh, edit=self.gh.edit)
        self.assertEqual(r["labels_readback"], ["agent:blocked", "kind:设计", "wait:你拍"])
        A.transition(self.key, "executing", authorized_scope="s")
        A.transition(self.key, "verifying")
        A.transition(self.key, "done", evidence="e")
        r = A.sync(self.key, gh=self.gh, edit=self.gh.edit)
        self.assertEqual(r["labels_readback"], ["kind:设计"])
        self.assertTrue(r["verified"])
        self.assertEqual(sorted(self.gh.created), ["agent:blocked", "agent:claimed"])

    def test_missing_label_is_created_before_edit(self):
        A.transition(self.key, "executing", authorized_scope="s")
        A.sync(self.key, gh=self.gh, edit=self.gh.edit)
        self.assertEqual(self.gh.created, ["agent:claimed"])
        self.assertTrue(any(p.startswith(f"repos/{R}/labels?") for p in self.gh.calls))  # 先只读查过
        A.transition(self.key, "verifying")
        A.sync(self.key, gh=self.gh, edit=self.gh.edit)
        self.assertEqual(self.gh.created, ["agent:claimed"])  # 已存在不重复建

    def test_cli_writeback_failure_is_clear_error_not_crash(self):
        def boom(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="HTTP 403 FIXTURE")
        old = (A.gh_json, A.run_cmd)
        A.gh_json, A.run_cmd = self.gh, boom
        try:
            rc = A.cli(["claim", "--key", self.key, "--owner-session", "o", "--task-id", "t", "--scope", "s",
                        "--writeback"])
        finally:
            A.gh_json, A.run_cmd = old
        self.assertEqual(rc, 2)
        self.assertEqual(self.jobs()["jobs"][self.key]["state"], "executing")  # 台账已落，label 待补

    def test_blank_scope_rejected(self):
        for s in ("", "   ", "\n\t"):
            with self.assertRaises(SystemExit):
                A.transition(self.key, "executing", authorized_scope=s)
        self.assertEqual(self.jobs()["jobs"][self.key]["state"], "pending")
        j, _ = A.transition(self.key, "executing", authorized_scope="  仅期0  ")
        self.assertEqual(j["authorized_scope"], "仅期0")

    def test_event_logged_only_after_ledger_commit(self):
        _, path = A.paths()

        def ev():
            with open(path, encoding="utf-8") as f:
                return f.read()
        before = ev()
        real = A.os.replace

        def fail(*a):
            raise OSError("FIXTURE disk full")
        A.os.replace = fail
        try:
            with self.assertRaises(OSError):
                A.transition(self.key, "blocked", reason="r")
        finally:
            A.os.replace = real
        self.assertEqual(ev(), before)
        self.assertEqual(self.jobs()["jobs"][self.key]["state"], "pending")
        A.transition(self.key, "blocked", reason="r")
        self.assertIn("-> blocked", ev())


class TestFixes(Base):
    def test_owner_needs_full_token(self):
        con = sqlite3.connect(self.db)
        t = dt.datetime(2026, 10, 24, 10, tzinfo=dt.timezone.utc).timestamp()
        con.executemany("insert into sessions values (?,?)", [("decoy", "desktop"), ("real", "desktop")])
        con.executemany("insert into messages (session_id, content, tool_calls, timestamp) values (?,?,?,?)", [
            ("decoy", "menxia-mac-plan 草稿", None, t), ("decoy", "x-menxia-mac 旧分支", None, t),
            ("decoy", "见 review/pull/1901", None, t), ("decoy", "review/pull/1902", None, t),
            ("real", "push origin/menxia-mac 完", None, t), ("real", None, '{"url":"review/pull/190."}', t),
            ("real", "折menxia-mac已呈", None, t),  # 中文紧贴也算完整 token
        ])
        con.commit()
        con.close()
        A.scan(gh=FakeGH([pr(190, "2026-10-24T20:00:00Z", ref="menxia-mac")]))
        j = next(iter(self.jobs()["jobs"].values()))
        self.assertEqual(j["owner_session"], "real")
        self.assertNotIn("decoy", j["owner_reason"])
        self.assertIn("('real', 3)", j["owner_reason"])

    def test_comments_paginated(self):
        cs = [{"id": i, "created_at": f"2026-10-24T19:{i // 60:02d}:{i % 60:02d}Z", "body": f"判 {i}"} for i in range(150)]
        A.scan(gh=FakeGH([pr(190, "2026-10-24T20:21:55Z")], {190: cs}))
        j = next(iter(self.jobs()["jobs"].values()))
        self.assertEqual(len(j["decisions"]), 150)

    def test_scan_writeback_matches_merge_sha(self):
        A.scan(gh=FakeGH([pr(190, "2026-10-24T20:21:55Z")]))
        self.clock += dt.timedelta(minutes=5)
        synced, real = [], A.sync
        A.sync = lambda k, gh=None, edit=None: synced.append(k)
        try:
            A.scan(gh=FakeGH([pr(190, "2026-10-24T20:21:55Z"), pr(190, "2026-10-24T20:33:00Z", sha="f" * 40)]),
                   writeback=True)
        finally:
            A.sync = real
        self.assertEqual(synced, [f"{R}#190@{'f' * 40}"])


if __name__ == "__main__":
    unittest.main()
