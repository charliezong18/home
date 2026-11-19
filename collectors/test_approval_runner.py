"""approval_runner.py / approvals_digest.py / approvals 服务来源 单测。
全部 FIXTURE：临时状态目录、假 claude/hd/gh/服务；不碰网络、真台账、真派工。
"""
import base64
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approvals as A  # noqa: E402
import approval_runner as R  # noqa: E402
import approvals_digest as D  # noqa: E402

SINCE = "2026-11-18T00:00:00Z"
SCOPE_OK = json.dumps({"type": "result", "subtype": "success", "result": "```json\n" + json.dumps(
    {"executable_steps": ["在 home 仓加 X 功能并开 PR"], "gated_steps": [], "repos": ["home"],
     "acceptance": ["pytest 全绿"], "notes": ""}, ensure_ascii=False) + "\n```"}, ensure_ascii=False)


def job(n, merged_at, kind="设计", state="pending"):
    k = f"{A.REPO}#{n}@{n:040d}"
    return {"key": k, "repo": A.REPO, "pr": n, "title": f"FIXTURE #{n}", "merge_sha": f"{n:040d}",
            "merged_at": merged_at, "labels_at_merge": [f"kind:{kind}", "proj:home"], "state": state,
            "state_zh": A.STATES[state], "authorized_scope": None, "pending_decisions": "1) 做 X？",
            "decisions": [{"id": 1, "where": "conversation", "at": merged_at, "body": "1) 做"}],
            "owner_session": None, "task_id": None, "evidence": None, "block_reason": None,
            "history": [{"at": merged_at, "to": "pending", "by": "scan"}], "event_ids": [], "writeback": None}


class FakeDeps:
    def __init__(self, tmp):
        self.tmp, self.scope_out, self.alive = tmp, SCOPE_OK, True
        self.creates, self.launches, self.gh_calls, self.svc_calls, self.scopes = [], [], [], [], 0
        self.launch_fail = False
        self.tasks = set()
        self.labels = {}

    def gh_json(self, path):
        if "/files" in path:
            return [{"filename": "docs/plan.md", "status": "added"}] if path.endswith("page=1") else []
        if "/contents/" in path:
            return {"content": base64.b64encode("# 方案\n做 X".encode()).decode()}
        if "/labels" in path:
            return [{"name": l} for l in ("wait:agent", "agent:claimed", "agent:blocked", "wait:你拍")]
        n = int(path.rsplit("/", 1)[1])
        return {"labels": [{"name": l} for l in sorted(self.labels.get(n, {"kind:设计"}))]}

    def gh_edit(self, cmd):
        if cmd[1] == "pr":
            n, cur = int(cmd[3]), set(self.labels.get(int(cmd[3]), {"kind:设计"}))
            for i, c in enumerate(cmd):
                if c == "--add-label":
                    cur.add(cmd[i + 1])
                if c == "--remove-label":
                    cur.discard(cmd[i + 1])
            self.labels[n] = cur

    def scope(self, prompt, prompt_file, timeout):
        self.scopes += 1
        self.last_prompt = prompt
        return self.scope_out

    def hd(self, args):
        if args[0] == "create":
            tid = args[args.index("--task-id") + 1]
            if tid in self.tasks:
                return 3, "", '{"error": "exists"}'
            self.tasks.add(tid)
            self.creates.append(args)
            run = os.path.join(self.tmp, "runs", tid)
            return 0, json.dumps({"dispatch": {"command": f"echo {tid}", "output_json": run + ".json",
                                               "exit_file": run + ".json.exit"}}), ""
        return 0, "{}", ""

    def launch(self, command, log_file):
        if self.launch_fail:
            raise OSError("FIXTURE launch crash")
        self.launches.append(command)
        return 4242

    def pid_alive(self, pid):
        return self.alive

    def gh(self, args):
        self.gh_calls.append(args)
        return ""

    def service_http(self, method, path, token, body=None):
        self.svc_calls.append((method, path, body))
        return {}

    def service_text(self, path, token):
        return "# doc"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory()
        self.tmp = self.tmpd.name
        A.STATE_DIR = os.path.join(self.tmp, "state")
        A.SERVICE_TOKEN_FILE = os.path.join(self.tmp, "no.token")
        os.makedirs(os.path.join(self.tmp, "runs"))
        self.cfg = dict(R.DEFAULTS, enabled=True, autoexec_since=SINCE, outputs_dir=os.path.join(self.tmp, "out"))
        self.d = FakeDeps(self.tmp)

    def tearDown(self):
        self.tmpd.cleanup()

    def seed(self, *jobs):
        os.makedirs(A.STATE_DIR, exist_ok=True)
        with open(os.path.join(A.STATE_DIR, "approvals.json"), "w") as f:
            json.dump({"cursor": None, "jobs": {j["key"]: j for j in jobs}, "references": {}}, f, ensure_ascii=False)

    def jobs(self):
        return R.read_ledger()["jobs"]

    def finish_worker(self, key, result, subtype="success", denials=None, rc=0):
        disp = R.load_json(os.path.join(R.work_dir(key), "dispatch.json"))
        with open(disp["output_json"], "w") as f:
            json.dump({"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": result,
                       "permission_denials": denials or []}, f, ensure_ascii=False)
        with open(disp["exit_file"], "w") as f:
            f.write(f"{rc}\n")


class TestScopeParse(unittest.TestCase):
    def test_outer_claude_json_and_fence(self):
        s = R.parse_scope(SCOPE_OK)
        self.assertEqual(s["executable_steps"], ["在 home 仓加 X 功能并开 PR"])
        self.assertEqual(s["acceptance"], ["pytest 全绿"])

    def test_gated_moved_out(self):
        s = R.parse_scope(json.dumps({"executable_steps": ["买域名 example.com 并付款", "改 README", "发邮件通知房东",
                                                           "提交 App Store 审核", "publish the npm package"],
                                      "gated_steps": ["签署合同"]}, ensure_ascii=False))
        self.assertEqual(s["executable_steps"], ["改 README"])
        self.assertEqual(len(s["gated_steps"]), 5)
        self.assertIn("签署合同", s["gated_steps"])

    def test_garbage(self):
        with self.assertRaises(ValueError):
            R.parse_scope("我觉得应该先问问 Charlie")


class TestRunner(Base):
    def test_old_backlog_never_runs(self):
        self.seed(job(100, "2026-10-24T00:00:00Z"), job(101, "2026-11-17T23:59:59Z"))
        self.assertEqual(R.run_once(self.cfg, self.d), "")
        self.assertEqual((self.d.scopes, self.d.creates), (0, []))
        self.assertTrue(all("autoexec" not in j for j in self.jobs().values()))

    def test_no_since_configured_runs_nothing(self):
        self.seed(job(102, "2026-11-18T10:00:00Z"))
        self.assertEqual(R.run_once(dict(self.cfg, autoexec_since=None), self.d), "")
        self.assertEqual(self.d.scopes, 0)

    def test_kind_filter_and_disabled(self):
        self.seed(job(103, "2026-11-18T10:00:00Z", kind="交付"))
        self.assertEqual(R.run_once(self.cfg, self.d), "")
        self.seed(job(104, "2026-11-18T10:00:00Z"))
        self.assertEqual(R.run_once(dict(self.cfg, enabled=False), self.d), "")
        self.assertEqual(self.d.scopes, 0)

    def test_pause_file(self):
        self.seed(job(105, "2026-11-18T10:00:00Z"))
        open(R.pause_file(), "w").close()
        self.assertEqual(R.run_once(self.cfg, self.d), "")
        self.assertEqual(self.d.scopes, 0)

    def test_daily_limit(self):
        today = A.iso(A.now())
        done = [dict(job(110 + i, "2026-11-18T01:00:00Z", state="done"),
                     autoexec={"phase": "closed", "started_at": today}) for i in range(3)]
        self.seed(*done, job(120, "2026-11-18T10:00:00Z"))
        self.assertEqual(R.run_once(self.cfg, self.d), "")
        self.assertEqual(self.d.scopes, 0)
        self.assertIn("每日上限", R.plan(self.cfg))

    def test_start_then_concurrency_one_and_idempotent(self):
        a, b = job(130, "2026-11-18T10:00:00Z"), job(131, "2026-11-18T11:00:00Z")
        self.seed(a, b)
        out = R.run_once(self.cfg, self.d)
        self.assertIn("开始", out)
        j = self.jobs()[a["key"]]
        self.assertEqual((j["state"], j["autoexec"]["phase"]), ("executing", "launched"))
        self.assertEqual(j["task_id"], R.task_id(a["key"]))
        self.assertIn("在 home 仓加 X 功能", j["authorized_scope"])
        self.assertIn("做 X", self.d.last_prompt)  # 文档原文进了定范围提示
        brief = self.d.creates[0][self.d.creates[0].index("--context") + 1]
        for must in ("批准原文", "gated", "menxia-vm.sh", "第 50 回合", "AUTOEXEC_STATUS", "不合并"):
            self.assertIn(must, brief)
        self.assertEqual(self.d.creates[0][:4], ["create", "--template", "build", "--complex"])
        for _ in range(3):  # worker 还在跑：后续轮次静默，不重复建任务，不开第二单
            self.assertEqual(R.run_once(self.cfg, self.d), "")
        self.assertEqual((len(self.d.creates), len(self.d.launches), self.d.scopes), (1, 1, 1))
        self.assertEqual(self.jobs()[b["key"]]["state"], "pending")

    def test_crash_resume(self):
        a = job(140, "2026-11-18T10:00:00Z")
        self.seed(a)
        self.d.launch_fail = True
        out = R.run_once(self.cfg, self.d)
        self.assertIn("下轮重试", out)
        j = self.jobs()[a["key"]]
        self.assertEqual((j["state"], j["autoexec"]["phase"]), ("executing", "dispatched"))
        self.d.launch_fail = False
        R.run_once(self.cfg, self.d)
        j = self.jobs()[a["key"]]
        self.assertEqual(j["autoexec"]["phase"], "launched")
        self.assertEqual((len(self.d.creates), len(self.d.launches), self.d.scopes), (1, 1, 1))

    def test_done_path_reports_and_publishes(self):
        a = job(150, "2026-11-18T10:00:00Z")
        self.seed(a)
        R.run_once(self.cfg, self.d)
        self.finish_worker(a["key"], '报告……\nAUTOEXEC_STATUS: {"verdict": "done", "tests_passed": true, '
                                     '"prs": ["https://github.com/x/y/pull/9"], "summary": "X 已上 PR"}')
        out = R.run_once(self.cfg, self.d)
        self.assertIn("完成", out)
        j = self.jobs()[a["key"]]
        self.assertEqual(j["state"], "done")
        self.assertEqual([h["to"] for h in j["history"]], ["pending", "executing", "verifying", "done"])
        rep = open(j["autoexec"]["report"], encoding="utf-8").read()
        for must in ("一句话结论", "X 已上 PR", "pull/9", "验证证据", "需要 Charlie 做的"):
            self.assertIn(must, rep)
        self.assertEqual(self.d.gh_calls[0][:3], ["pr", "comment", "150"])
        self.assertTrue(self.d.gh_calls[0][-1].startswith("**回话**"))  # approvals.fetch_decisions 会忽略
        pub = self.d.creates[-1]
        self.assertIn("mcp__menxia__open_folder", pub)
        self.assertIn("sonnet", pub)
        self.assertIn("proj:home", pub[pub.index("--question") + 1])
        self.assertTrue(j["writeback"]["verified"])
        self.assertEqual(R.run_once(self.cfg, self.d), "")  # 收尾后不再重复评论
        self.assertEqual(len(self.d.gh_calls), 1)

    def test_denials_block(self):
        a = job(160, "2026-11-18T10:00:00Z")
        self.seed(a)
        R.run_once(self.cfg, self.d)
        self.finish_worker(a["key"], 'AUTOEXEC_STATUS: {"verdict": "done", "tests_passed": true, "summary": "好"}',
                           denials=[{"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}])
        out = R.run_once(self.cfg, self.d)
        j = self.jobs()[a["key"]]
        self.assertEqual(j["state"], "blocked")
        self.assertIn("权限被拒", j["block_reason"])
        self.assertIn("卡住", out)

    def test_tests_failed_or_missing_status_block(self):
        for n, res in ((170, 'AUTOEXEC_STATUS: {"verdict": "done", "tests_passed": false, "summary": "s"}'),
                       (171, "做完了，没给结构化结论")):
            self.setUp()
            a = job(n, "2026-11-18T10:00:00Z")
            self.seed(a)
            R.run_once(self.cfg, self.d)
            self.finish_worker(a["key"], res)
            R.run_once(self.cfg, self.d)
            self.assertEqual(self.jobs()[a["key"]]["state"], "blocked", n)

    def test_worker_lost_blocks(self):
        a = job(180, "2026-11-18T10:00:00Z")
        self.seed(a)
        R.run_once(self.cfg, self.d)
        self.d.alive = False
        R.run_once(self.cfg, self.d)
        j = self.jobs()[a["key"]]
        self.assertEqual(j["state"], "blocked")
        self.assertIn("进程已不在", j["block_reason"])

    def test_no_executable_steps_blocks_with_report(self):
        a = job(190, "2026-11-18T10:00:00Z")
        self.seed(a)
        self.d.scope_out = json.dumps({"executable_steps": ["给房东发邮件"], "gated_steps": [], "notes": "只有对外动作"},
                                      ensure_ascii=False)
        out = R.run_once(self.cfg, self.d)
        j = self.jobs()[a["key"]]
        self.assertEqual(j["state"], "blocked")
        self.assertEqual(self.d.creates[0][self.d.creates[0].index("--task-id") + 1], R.task_id(a["key"], "axpub"))
        self.assertEqual(len([c for c in self.d.creates if "--complex" in c]), 0)  # 没派执行 worker
        self.assertIn("给房东发邮件", open(j["autoexec"]["report"], encoding="utf-8").read())
        self.assertIn("卡住", out)

    def test_dry_run_writes_nothing(self):
        self.seed(job(200, "2026-10-24T00:00:00Z"), job(201, "2026-11-18T10:00:00Z"))
        before = open(os.path.join(A.STATE_DIR, "approvals.json")).read()
        out = R.run_once(self.cfg, self.d, dry_run=True)
        self.assertIn("旧积压", out)
        self.assertIn("会开始", out)
        self.assertEqual(before, open(os.path.join(A.STATE_DIR, "approvals.json")).read())
        self.assertEqual((self.d.scopes, self.d.creates), (0, []))


class TestServiceSource(Base):
    def token(self):
        p = os.path.join(self.tmp, "runner.token")
        with open(p, "w") as f:
            f.write("FIXTURE")
        os.chmod(p, 0o600)
        A.SERVICE_TOKEN_FILE = p

    def http(self, pages):
        calls = []

        def h(method, path, token, body=None):
            calls.append(path)
            if path.startswith("/api/v1/events"):
                return pages.pop(0)
            if path.endswith("/decisions"):
                return {"items": [{"id": "dc_1", "verdict": "approve", "created_at": "2026-11-18T10:00:00Z"}]}
            return {"title": "服务折", "labels": ["kind:拍板"], "questions": [{"text": "做吗？"}],
                    "web_url": "https://menxia-service.menxia-service.workers.dev/r/rv_1"}
        h.calls = calls
        return h

    def test_scan_ingests_approve_only_and_is_idempotent(self):
        self.token()
        recent = A.iso(A.now())
        ev = lambda t, did, verdict: {"review_id": "rv_1", "type": t, "ref_id": did, "created_at": recent,
                                      "data": {"verdict": verdict, "revision_no": 2}}
        h = self.http([{"items": [ev("comment.created", "c1", None), ev("decision.created", "dc_0", "request_changes")],
                        "next_cursor": "c-1", "has_more": True},
                       {"items": [ev("decision.created", "dc_1", "approve")], "next_cursor": "c-2", "has_more": False}])
        with A.ledger() as data:
            line = A.scan_service(data, h)
        self.assertIn("new_jobs=['rv_1']", line)
        k = "menxia-service#rv_1@dc_1"
        with A.ledger() as data:
            self.assertEqual(data["service_cursor"], "c-2")
            j = data["jobs"][k]
            self.assertEqual((j["source"], j["state"], j["revision_no"]), ("menxia-service", "pending", 2))
            self.assertIn("做吗", j["pending_decisions"])
            line = A.scan_service(data, self.http([{"items": [ev("decision.created", "dc_1", "approve")],
                                                    "next_cursor": "c-3", "has_more": False}]))
        self.assertIn("dup=['rv_1']", line)
        self.assertEqual(A.sync(k)["skipped"], "menxia-service 来源无 label 回写")

    def test_no_token_skips_quietly(self):
        with A.ledger() as data:
            line = A.scan_service(data, lambda *a, **k: self.fail("不该调服务"))
        self.assertIn("跳过", line)
        self.assertIn("token 不存在", line)

    def test_loose_token_permissions_skip(self):
        self.token()
        os.chmod(A.SERVICE_TOKEN_FILE, 0o644)
        self.assertIn("600", A.service_token()[1])

    def test_runner_publishes_service_comment(self):
        self.token()
        j = dict(job(0, "2026-11-18T10:00:00Z", kind="拍板"), key="menxia-service#rv_9@dc_9", source="menxia-service",
                 repo="menxia-service", pr=None, review_id="rv_9", decision_id="dc_9", revision_no=1, merge_sha=None)
        self.seed(j)
        self.d.scope_out = json.dumps({"executable_steps": [], "notes": "全是讨论"}, ensure_ascii=False)
        R.run_once(self.cfg, self.d)
        got = self.jobs()[j["key"]]
        self.assertEqual(got["state"], "blocked")
        posts = [c for c in self.d.svc_calls if c[0] == "POST"]
        self.assertEqual(posts[0][1], "/api/v1/reviews/rv_9/comments")
        self.assertEqual(self.d.gh_calls, [])


class TestDigest(Base):
    def test_cursor(self):
        a = job(300, "2026-11-18T10:00:00Z")
        self.seed(a, job(301, "2026-10-01T00:00:00Z"))
        self.assertEqual(D.digest(self.cfg), "")  # 首次：since 之后没有开始/完成/卡住
        R.run_once(self.cfg, self.d)
        out = D.digest(self.cfg)
        self.assertIn("开始执行：FIXTURE #300", out)
        self.assertIn("https://menxia-reader.github.io/menxia/?pr=300", out)
        self.assertEqual(D.digest(self.cfg), "")  # 游标前移，无变化静默
        self.d.alive = False
        R.run_once(self.cfg, self.d)
        out = D.digest(self.cfg)
        self.assertIn("卡住：FIXTURE #300", out)
        self.assertIn("RESULT.zh-CN.md", out)
        self.assertNotIn("开始执行", out)

    def test_peek_does_not_advance(self):
        self.seed(dict(job(310, "2026-11-18T10:00:00Z", state="blocked"),
                       history=[{"at": "2026-11-18T10:00:00Z", "to": "pending"},
                                {"at": "2026-11-18T10:05:00Z", "to": "blocked", "reason": "r"}]))
        self.assertIn("卡住", D.digest(self.cfg, advance=False))
        self.assertIn("卡住", D.digest(self.cfg))
        self.assertEqual(D.digest(self.cfg), "")


if __name__ == "__main__":
    unittest.main()
