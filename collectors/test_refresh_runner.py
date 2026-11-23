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


class FakeService:
    def __init__(self, revs):
        self.revs = list(revs)  # 每次 GET 详情依次返回的 current_revision_no
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
                    "decider": {"id": "usr_charlie"}, "labels": []}
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
