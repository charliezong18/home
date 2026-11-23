"""门下「更新」单（kind=refresh，服务 D-19）：入账、幂等、running→done 需要新版本证据、failed、与画可单共享并发、日上限分开。"""
import json
import os

import pytest

import approval_runner as R
import approvals as A

RID, REQ = "rv_01REFRESHTEST000000000000", "rfr_01REFRESHTEST00000000000"
SINCE = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "STATE_DIR", str(tmp_path / "state"))
    tok = tmp_path / "runner.token"
    tok.write_text("mxa_" + "x" * 40)
    os.chmod(tok, 0o600)
    monkeypatch.setattr(A, "SERVICE_TOKEN_FILE", str(tok))
    monkeypatch.setattr(A, "service_token", lambda path=None: ("mxa_" + "x" * 40, None))
    os.makedirs(A.STATE_DIR, exist_ok=True)
    return tmp_path


DOCS = ["docs/x.zh-CN.md", "docs/x.en.md"]


class FakeService:
    def __init__(self, revs, docs=DOCS):
        self.revs = list(revs)  # 每次 GET 详情依次返回的 current_revision_no
        self.docs = list(docs)
        self.calls = []

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/api/v1/events"):
            return {"items": [{"review_id": RID, "type": "refresh.requested", "ref_id": REQ,
                               "data": {"request_id": REQ, "status": "queued"}, "created_at": A.iso(A.now())}],
                    "next_cursor": "c1", "has_more": False}
        if method == "GET" and path.endswith("/refresh-requests"):
            return {"items": [{"request_id": REQ, "status": "queued", "note": "核对价格"}]}
        if method == "GET" and path == f"/api/v1/reviews/{RID}":
            n = self.revs.pop(0) if len(self.revs) > 1 else self.revs[0]
            return {"title": "会过时的折", "web_url": f"https://x/r/{RID}", "current_revision_no": n,
                    "decider": {"id": "usr_charlie"}, "labels": [],
                    "current_revision": {"revision_no": n, "documents": [{"path": p} for p in self.docs]}}
        if method == "PATCH":
            return {"request_id": REQ, **(body or {})}
        raise AssertionError(f"unexpected {method} {path}")

    def patches(self):
        return [b for m, _, b in self.calls if m == "PATCH"]


class FakeDeps(R.Deps):
    def __init__(self, svc, alive_tid=None):
        super().__init__()
        self.service_http = svc
        self.alive_tid = alive_tid
        self.texts, self.fail_paths = [], set()

    def service_text(self, path, token):
        assert token.startswith("mxa_")
        self.texts.append(path)
        if any(path.endswith("/content/" + R.urllib.parse.quote(p)) for p in self.fail_paths):
            raise OSError("HTTP Error 404: Not Found")
        return f"原文 {path}"

    def selftest(self):
        return {"ok": True, "checks": {}, "detail": "fake"}

    def proc_cmd(self, pid):
        return f"claude {self.alive_tid}" if self.alive_tid else ""


def cfg(**kw):
    return R.normalize_config(dict({"enabled": True, "autoexec_since": SINCE, "service_admin_ids": ["usr_charlie"]}, **kw))


def ingest(svc):
    with A.ledger() as data:
        line = A.scan_service(data, svc)
    return line


def test_ingest_and_idempotent(state):
    svc = FakeService([3])
    line = ingest(svc)
    key = A.refresh_key(RID, REQ)
    assert key == f"menxia-service#{RID}@refresh:{REQ}"
    assert RID in line and "new_jobs" in line
    j = R.read_ledger()["jobs"][key]
    assert (j["kind"], j["state"], j["note"], j["revision_no_at_request"]) == ("refresh", "pending", "核对价格", 3)
    # 再扫一次同一事件：dup，不新建
    with A.ledger() as data:
        assert A.ingest_refresh(data, svc("GET", "/api/v1/events", "t")["items"][0], svc, "t") == "dup"
    assert len(R.read_ledger()["jobs"]) == 1
    assert R.ineligible_reason(j, cfg()) is None
    assert "service_admin_ids" in R.ineligible_reason(j, cfg(service_admin_ids=["usr_other"]))


def _claimed_at_verify(svc, base):
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    assert R.claim_refresh(key)["state"] == "executing"
    assert R.claim_refresh(key) is None  # CAS：第二次抢不到
    R.patch(key, phase="r_verify", base_revision_no=base, worker="ok")
    return key


def test_running_to_done_requires_new_revision(state):
    svc = FakeService([3, 4])  # 入账时 v3；核验时 v4
    key = _claimed_at_verify(svc, 3)
    out = R.advance(R.read_ledger()["jobs"][key], cfg(), FakeDeps(svc))
    assert out == f"门下更新：已更新：会过时的折（v4）https://x/r/{RID}"
    assert svc.patches()[-1] == {"status": "done", "result_revision_no": 4}
    j = R.read_ledger()["jobs"][key]
    assert j["state"] == "done" and j["autoexec"]["phase"] == "closed" and "3 → 4" in j["evidence"]


def test_no_new_revision_is_failed(state):
    svc = FakeService([3, 3])
    key = _claimed_at_verify(svc, 3)
    out = R.advance(R.read_ledger()["jobs"][key], cfg(), FakeDeps(svc))
    assert out.startswith("门下更新：未更新 会过时的折")
    last = svc.patches()[-1]
    assert last["status"] == "failed" and "没有新版本" in last["error"]
    assert "done" not in [p["status"] for p in svc.patches()]
    assert R.read_ledger()["jobs"][key]["state"] == "blocked"


def test_shared_concurrency_and_separate_daily_limits(state):
    svc = FakeService([3])
    ingest(svc)
    rkey = A.refresh_key(RID, REQ)
    R.claim_refresh(rkey)
    R.patch(rkey, phase="r_launched", pid=12345, launched_at=A.iso(A.now()))
    tid = R.read_ledger()["jobs"][rkey]["autoexec"]["task_id"]
    # 一张合格的画可单（GitHub 来源）
    akey = "charliezong18/review#900@" + "a" * 40
    with A.ledger() as data:
        data["jobs"][akey] = {"key": akey, "pr": 900, "title": "画可单", "state": "pending", "merged_by": A.CHARLIE,
                              "merged_at": A.iso(A.now()), "labels_at_merge": ["kind:设计"], "history": [{"at": A.iso(A.now())}],
                              "event_ids": []}
    jobs = R.read_ledger()["jobs"]
    assert R.ineligible_reason(jobs[akey], cfg(), jobs) is None
    assert R.active_job(jobs)["key"] == rkey
    with open(os.path.join(A.STATE_DIR, "selftest.json"), "w") as f:
        json.dump({"ok": True, "date": A.now().astimezone().date().isoformat(), "checks": {}, "detail": "fake"}, f)
    out = R.run_once(cfg(), FakeDeps(svc, alive_tid=tid))  # refresh worker 还活着 → 本轮只等它
    assert out == ""
    assert R.read_ledger()["jobs"][akey]["state"] == "pending"
    assert "autoexec" not in R.read_ledger()["jobs"][akey]
    # 日上限分开：refresh 已开 1 单不占画可单的 daily_limit=1
    jobs = R.read_ledger()["jobs"]
    assert R.started_today(jobs, refresh=True) == 1 and R.started_today(jobs) == 0
    assert R.under_limit(jobs[akey], jobs, cfg(daily_limit=1))
    assert not R.under_limit(dict(jobs[rkey], autoexec=None), jobs, cfg(refresh_daily_limit=1))


class LaunchDeps(FakeDeps):
    """hd create 返回带 claude argv 的派工；launch 只记录不真起进程。"""

    def __init__(self, svc, tmp):
        super().__init__(svc)
        self.tmp, self.launches = str(tmp), []

    def hd(self, args):
        if args[0] == "create":
            tid = args[args.index("--task-id") + 1]
            run = os.path.join(self.tmp, "runs", tid)
            argv = ["claude", "-p", "--allowedTools", "Read", "--", "PROMPT"]
            cmd = f"cd x && {' '.join(argv[:-1])} \"$(cat p)\""
            return 0, json.dumps({"dispatch": {"command": cmd, "argv": argv, "output_json": run + ".json",
                                               "exit_file": run + ".json.exit"}}), ""
        return 0, "{}", ""

    def launch(self, command, log_file):
        self.launches.append(command)
        return 4242


def _selftest_ok():
    with open(os.path.join(A.STATE_DIR, "selftest.json"), "w") as f:
        json.dump({"ok": True, "date": A.now().astimezone().date().isoformat(), "checks": {}, "detail": "fake"}, f)


def test_refresh_claim_then_launch_starts_worker(state):
    """回归：refresh 单 claim 后 launch 前的 still_mine 守卫曾按画可单的 task_id(ax-) 比对 → 误判「已被他人接手」。"""
    svc = FakeService([3])
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    _selftest_ok()
    deps = LaunchDeps(svc, state)
    deps.alive_tid = R.task_id(key, "rf")  # 起来的 worker 还活着 → 停在 r_launched
    out = R.run_once(cfg(outputs_dir=str(state / "out")), deps)
    j = R.read_ledger()["jobs"][key]
    assert len(deps.launches) == 1, out
    assert j["state"] == "executing" and j["autoexec"]["phase"] == "r_launched" and j["autoexec"]["pid"] == 4242
    assert out.startswith("门下更新：开始 会过时的折")
    assert {"status": "running"} in svc.patches() and "failed" not in [p.get("status") for p in svc.patches()]


def test_refresh_taken_over_before_launch_no_worker(state):
    """refresh 单同样受抢单保护：launch 前被 Hermes 改了 owner → 不启动 worker。"""
    svc = FakeService([3])
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    R.claim_refresh(key)
    real_hd = LaunchDeps.hd

    def racing(self, args):
        with A.ledger() as data:
            data["jobs"][key].update(owner_session="hermes-s", task_id="t-hermes")
        return real_hd(self, args)
    deps = LaunchDeps(svc, state)
    deps.hd = racing.__get__(deps)
    R.do_refresh_dispatch(R.read_ledger()["jobs"][key], dict(cfg(outputs_dir=str(state / "out")), _selftest_ok=True), deps)
    assert deps.launches == []
    assert R.read_ledger()["jobs"][key]["autoexec"]["phase"] == "closed"


def test_still_mine_refresh_does_not_accept_approve_task_id(state):
    """approve 单的 task_id 不能冒充 refresh 单，反之亦然（守卫按单种类比对）。"""
    svc = FakeService([3])
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    R.claim_refresh(key)
    assert R.still_mine(key)
    with A.ledger() as data:
        data["jobs"][key]["task_id"] = R.task_id(key)  # ax- 前缀
    assert not R.still_mine(key)


def _stuck_refresh(svc):
    """复现线上卡死单：executing + autoexec{phase: closed, kind: refresh} + 未启动 worker。"""
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    R.claim_refresh(key)
    R.patch(key, phase="closed", closed_at=A.iso(A.now()), note="launch 前发现单子已被他人接手/改状态，未启动 worker")
    return key


def test_stuck_refresh_recovered_to_failed_and_blocked(state):
    svc = FakeService([3])
    key = _stuck_refresh(svc)
    _selftest_ok()
    out = R.run_once(cfg(outputs_dir=str(state / "out")), LaunchDeps(svc, state))
    assert {"status": "failed", "error": "执行器内部错误，请重试"} in svc.patches()
    j = R.read_ledger()["jobs"][key]
    assert j["state"] == "blocked" and j["autoexec"]["phase"] == "closed"
    assert j["history"][-1]["by"] == "autoexec" and j["history"][-1]["to"] == "blocked"
    assert "会过时的折" in out
    # 进 digest：报「没更新成」
    import approvals_digest as D
    assert "没更新成" in D.line(j, j["history"][-1])
    # 再跑一轮：不重复 PATCH
    n = len(svc.patches())
    R.run_once(cfg(outputs_dir=str(state / "out")), LaunchDeps(svc, state))
    assert len(svc.patches()) == n


def test_stuck_recovery_ignores_job_owned_by_others(state):
    svc = FakeService([3])
    key = _stuck_refresh(svc)
    with A.ledger() as data:
        data["jobs"][key].update(owner_session="hermes-s", task_id="t-hermes")
    _selftest_ok()
    R.run_once(cfg(outputs_dir=str(state / "out")), LaunchDeps(svc, state))
    assert "failed" not in [p.get("status") for p in svc.patches()]
    assert R.read_ledger()["jobs"][key]["state"] == "executing"


# ---------- 回归（真实联调第二轮，2026-11-22）：refresh worker 启动即死 ----------
# 线上 key 很长，slug 截到 60 字符后同一折的不同「更新」请求共用 work_dir/out_dir → 复用上一张单的 dispatch.json
# （旧 task_id c3717fdd05）→ 跑起来的是旧单的命令，台账记的却是新 task_id → worker_alive 判死；且 launch 与
# check 在同一轮 advance 里连跑，1 秒内就判 failed。
LONG_RID = "rv_01M77A6P283ZG4N2EKHZXC5QRS"
LONG_REQ1, LONG_REQ2 = "rfr_01M7ZZZZZZZZZZZZZZZZZZZZZZ", "rfr_01M83CWC2WJ2KY24JQ4T24KHTP"


def test_long_refresh_keys_same_review_get_distinct_dirs(state):
    k1, k2 = A.refresh_key(LONG_RID, LONG_REQ1), A.refresh_key(LONG_RID, LONG_REQ2)
    assert R.slug(k1) != R.slug(k2) and len(R.slug(k2)) <= 60
    assert R.work_dir(k1) != R.work_dir(k2)
    c = cfg(outputs_dir="/tmp/o")
    assert R.out_dir({"key": k1}, c) != R.out_dir({"key": k2}, c)
    # task_id 不受影响（向后兼容：已在台账里的 task_id 照样对得上）
    assert R.task_id(k2, "rf") == "rf-menxia-service-rv_01M77A6P283ZG4N2EKHZXC-c9abdded70"
    # 短 key 的 slug 不变（在办单的 work_dir 不搬家）
    assert R.slug("charliezong18/review#214@" + "2de83f50b578" + "0" * 28) == "review-214-2de83f50b578"


def test_stale_dispatch_of_other_task_is_not_launched(state):
    """work_dir 里留着别的任务的 dispatch.json（task_id 不是本单）→ 绝不拿它启动。"""
    svc = FakeService([3])
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    R.claim_refresh(key)
    R.save_json(os.path.join(R.work_dir(key), "dispatch.json"),
                {"task_id": "rf-old-c3717fdd05", "command": "claude rf-old-c3717fdd05", "argv": [],
                 "exit_file": "/nonexistent.exit"})
    deps = LaunchDeps(svc, state)
    with pytest.raises(R.StepError, match="dispatch.json"):
        R.do_refresh_dispatch(R.read_ledger()["jobs"][key], dict(cfg(outputs_dir=str(state / "out")), _selftest_ok=True), deps)
    assert deps.launches == []


def test_launch_round_does_not_judge_worker_dead(state):
    """launch 的那一轮不检查存活：哪怕 ps 还看不到 worker，也停在 r_launched，不 PATCH failed。"""
    svc = FakeService([3])
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    _selftest_ok()
    deps = LaunchDeps(svc, state)  # alive_tid=None：ps 看不到
    out = R.run_once(cfg(outputs_dir=str(state / "out")), deps)
    j = R.read_ledger()["jobs"][key]
    disp = R.load_json(os.path.join(R.work_dir(key), "dispatch.json"))
    assert len(deps.launches) == 1 and disp["task_id"] == j["autoexec"]["task_id"] == j["task_id"] == R.task_id(key, "rf")
    assert j["autoexec"]["phase"] == "r_launched", j["autoexec"]
    assert "failed" not in [p.get("status") for p in svc.patches()]
    assert out.startswith("门下更新：开始")


def test_dead_worker_records_exit_code_and_stderr(state):
    """worker 起不来：退出码与 stderr / worker.log 尾巴写进台账 worker 与 error。"""
    svc = FakeService([3, 3])
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    _selftest_ok()
    deps = LaunchDeps(svc, state)
    R.run_once(cfg(outputs_dir=str(state / "out")), deps)
    disp = R.load_json(os.path.join(R.work_dir(key), "dispatch.json"))
    os.makedirs(os.path.dirname(disp["exit_file"]), exist_ok=True)
    with open(disp["exit_file"], "w") as f:
        f.write("127\n")
    with open(disp["exit_file"].replace(".json.exit", ".json.err"), "w") as f:
        f.write("bash: claude: command not found\n")
    with open(os.path.join(R.work_dir(key), "worker.log"), "w") as f:
        f.write("cron.env missing\n")
    R.run_once(cfg(outputs_dir=str(state / "out")), deps)
    j = R.read_ledger()["jobs"][key]
    a = j["autoexec"]
    assert a["phase"] == "closed"
    for s in ("127", "command not found", "cron.env missing"):
        assert s in a["worker"] and s in a["error"], (s, a)
    assert "command not found" in svc.patches()[-1]["error"]


def test_vanished_worker_without_exit_records_log_tail(state):
    svc = FakeService([3, 3])
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    _selftest_ok()
    deps = LaunchDeps(svc, state)
    R.run_once(cfg(outputs_dir=str(state / "out")), deps)
    with open(os.path.join(R.work_dir(key), "worker.log"), "w") as f:
        f.write("Error: not logged in\n")
    R.run_once(cfg(outputs_dir=str(state / "out")), deps)
    a = R.read_ledger()["jobs"][key]["autoexec"]
    assert "worker 已不在且无退出记录" in a["worker"] and "not logged in" in a["worker"] and "not logged in" in a["error"]


def test_refresh_worker_tools_and_argv():
    outd = "/tmp/rf-out"
    tools = R.refresh_tools(outd)
    assert "Bash" not in " ".join(tools) and "Read" in tools and "WebSearch" in tools
    assert f"Write({R.scoped(outd)})" in tools and "Write" not in tools
    argv = ["claude", "-p", "--strict-mcp-config", "--allowedTools", "Bash", "Edit", "--", "prompt"]
    new = R.rewrite_worker_argv([x for x in argv if x != "--strict-mcp-config"], "/s.json", tools, replace=True)
    i, j = new.index("--allowedTools"), new.index("--")
    assert new[i + 1:j] == tools and "--strict-mcp-config" not in new
    deny = R.refresh_settings(outd)["permissions"]["deny"]
    assert "WebFetch" not in deny and "WebSearch" not in deny and "Bash" in deny


# ---------- 回归（真实联调第三轮，2026-11-22）：worker 读不到原文 ----------
# get_review 只给元数据与文档清单、不含正文，白名单里也没有读正文的 menxia 工具 → worker（正确地）停了。
# 现在执行器在 launch 前用 mxa_ 令牌拉 v<n> 全部正文到 <outputs>/source/v<n>/<path>，prompt 列出这些路径。

class CtxDeps(LaunchDeps):
    def hd(self, args):
        if args[0] == "create":
            self.context = args[args.index("--context") + 1]
        return super().hd(args)


def _dispatch(state, svc, deps=None):
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    R.claim_refresh(key)
    deps = deps or CtxDeps(svc, state)
    c = dict(cfg(outputs_dir=str(state / "out")), _selftest_ok=True)
    out = R.do_refresh_dispatch(R.read_ledger()["jobs"][key], c, deps)
    return key, c, deps, out


def test_refresh_sources_fetched_and_written(state):
    svc = FakeService([3])
    key, c, deps, out = _dispatch(state, svc)
    src = R.source_dir(R.read_ledger()["jobs"][key], c, 3)
    assert src == os.path.join(R.out_dir({"key": key}, c), "source", "v3")
    for p in DOCS:
        with open(os.path.join(src, p), encoding="utf-8") as f:
            assert f.read() == f"原文 /api/v1/reviews/{RID}/revisions/3/content/{R.urllib.parse.quote(p)}"
    assert len(deps.launches) == 1 and out.startswith("门下更新：开始")
    assert os.path.isdir(os.path.join(R.out_dir({"key": key}, c), "out"))


def test_refresh_prompt_lists_source_paths(state):
    svc = FakeService([3])
    key, c, deps, _ = _dispatch(state, svc)
    outd = R.out_dir({"key": key}, c)
    for p in DOCS:
        assert f"`{os.path.join(outd, 'source', 'v3', p)}`" in deps.context
    assert os.path.join(outd, "out") + "/<同路径>" in deps.context
    assert "Glob" not in deps.context and "全部文档" in deps.context
    # prompt 函数本身：列出每个 source 路径
    b = R.refresh_brief({"key": key, "title": "t", "review_id": RID, "request_id": REQ, "source": A.SERVICE}, c, 7,
                        ["docs/a.md"])
    assert os.path.join(outd, "source", "v7", "docs/a.md") in b


@pytest.mark.parametrize("bad", ["../escape.md", "docs/../../x.md", "/etc/passwd", "docs//x.md", "docs\\x.md", ""])
def test_refresh_traversal_path_rejected(state, bad):
    assert R.safe_doc_path(bad) is None
    svc = FakeService([3], docs=["docs/ok.md", bad])
    key, c, deps, out = _dispatch(state, svc)
    assert deps.launches == [] and deps.texts == []  # 一篇都不拉、不启动
    j = R.read_ledger()["jobs"][key]
    assert j["state"] == "blocked" and j["autoexec"]["phase"] == "closed"
    last = svc.patches()[-1]
    assert last["status"] == "failed" and "不安全" in last["error"]
    assert not os.path.exists(os.path.join(str(state), "escape.md"))


def test_safe_doc_path_accepts_normal():
    assert R.safe_doc_path("docs/x.zh-CN.md") == "docs/x.zh-CN.md"


def test_refresh_fetch_failure_no_launch_and_failed(state):
    svc = FakeService([3])
    deps = CtxDeps(svc, state)
    deps.fail_paths = {"docs/x.en.md"}
    key, c, deps, out = _dispatch(state, svc, deps)
    assert deps.launches == []
    assert not os.path.exists(os.path.join(R.work_dir(key), "dispatch.json"))
    last = svc.patches()[-1]
    assert last["status"] == "failed" and "docs/x.en.md" in last["error"] and "拉取原文失败" in last["error"]
    j = R.read_ledger()["jobs"][key]
    assert j["state"] == "blocked" and j["autoexec"]["phase"] == "closed" and "docs/x.en.md" in j["autoexec"]["error"]
    assert out.startswith("门下更新：未更新")


def test_refresh_base_moved_no_launch(state):
    """台账记的 base 是 v3，服务里已是 v4：不拉 v4 冒充 v3，直接 failed。"""
    svc = FakeService([4])
    ingest(svc)
    key = A.refresh_key(RID, REQ)
    R.claim_refresh(key)
    R.patch(key, base_revision_no=3)
    deps = CtxDeps(svc, state)
    R.do_refresh_dispatch(R.read_ledger()["jobs"][key], dict(cfg(outputs_dir=str(state / "out")), _selftest_ok=True),
                          deps)
    assert deps.launches == [] and "v4" in svc.patches()[-1]["error"]
