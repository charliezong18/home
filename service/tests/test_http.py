import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402
import cli  # noqa: E402
import config  # noqa: E402
import dibao  # noqa: E402
import ledger  # noqa: E402


def req(port, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"null"), resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None), e.headers


def _cfg():
    cfg = config.load()
    cfg.worker_timeout = 3
    return cfg


def git_log():
    return _env.sh("git", "-C", _env.DATA, "log", "--format=%s")


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dibao.fetch_title = lambda url: ("假标题", True)
        cls.svc = dibao.Service(_cfg(), push=False)
        cls.svc.prepare_repo()
        cls.srv = dibao.make_server(cls.svc, 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_01_health(self):
        code, body, _ = req(self.port, "GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["pending_edits"], 0)
        self.assertEqual(body["version"], dibao.VERSION)

    def test_02_edit_applied_and_home_json_etag(self):
        code, body, _ = req(self.port, "POST", "/v1/edits",
                            {"id": "e:test1", "target": "p:test", "fields": {"next": "new"}, "from": "claude"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["edit"]["state"], "applied")
        self.assertEqual(body["project"]["next"], "new")       # 重算后的 home.json 已带新值
        self.assertTrue(body["generated"])
        self.assertEqual(ledger.read_ledger(dibao.EDITS)["e:test1"]["state"], "applied")
        self.assertIn("edits: apply 1", git_log())             # 本地已提交，工作树干净
        self.assertEqual(_env.sh("git", "-C", _env.DATA, "status", "--porcelain"), "")
        code, _, h = req(self.port, "GET", "/v1/home.json")
        self.assertEqual(code, 200)
        code, _, _ = req(self.port, "GET", "/v1/home.json", headers={"If-None-Match": h["ETag"]})
        self.assertEqual(code, 304)
        self.assertEqual(req(self.port, "GET", "/v1/status.json")[0], 200)

    def test_03_edit_idempotent_and_validation(self):
        code, body, _ = req(self.port, "POST", "/v1/edits", {"id": "e:test1", "target": "p:test", "fields": {"next": "x"}})
        self.assertEqual((code, body["edit"]["state"]), (200, "applied"))      # 同 id 不重放
        for bad in ({"target": "p:test", "fields": {"secret": "x"}},          # 不在白名单
                    {"target": "p:test", "fields": {"status": "bogus"}},
                    {"target": "p:test", "fields": {"next": "x"}, "from": "web"},
                    {"fields": {"next": "x"}}):
            self.assertEqual(req(self.port, "POST", "/v1/edits", bad)[0], 400, bad)

    def test_04_edit_unknown_target_failed(self):
        code, body, _ = req(self.port, "POST", "/v1/edits", {"target": "p:nope", "fields": {"next": "x"}, "from": "hermes"})
        self.assertEqual((code, body["edit"]["state"]), (200, "failed"))
        self.assertIn("找不到", body["edit"]["error"])

    def test_05_edit_timeout_returns_202_and_stays_pending(self):
        code, body, _ = req(self.port, "POST", "/v1/edits", {"id": "e:slow", "target": "p:slow", "fields": {"next": "x"}})
        self.assertEqual((code, body["edit"]["state"]), (202, "pending"))
        self.assertEqual(ledger.read_ledger(dibao.EDITS)["e:slow"]["state"], "pending")
        self.assertEqual(req(self.port, "GET", "/health")[1]["pending_edits"], 1)
        # 清掉这条，免得后面的测试每次都等超时：直接追加一条 failed 状态行
        ledger.append(dibao.EDITS, {"id": "e:slow", "state": "failed", "ts": "t"})
        _env.sh("git", "-C", _env.DATA, "commit", "-qam", "test: clear slow")

    def test_06_note_flow(self):
        code, body, _ = req(self.port, "POST", "/v1/edits",
                            {"id": "e:note", "target": "p:test", "fields": {"next": "n2"}, "note": "请 Hermes 看看"})
        self.assertEqual(body["edit"]["state"], "needs_hermes")
        code, body, _ = req(self.port, "GET", "/v1/notes/pending")
        self.assertEqual([e["id"] for e in body], ["e:note"])
        code, body, _ = req(self.port, "POST", "/v1/notes/e:note/done", {"result": "处理了"})
        self.assertEqual((code, body["state"]), (200, "done"))
        self.assertEqual(req(self.port, "GET", "/v1/notes/pending")[1], [])
        self.assertEqual(ledger.read_ledger(dibao.EDITS)["e:note"]["result"], "处理了")
        self.assertEqual(req(self.port, "POST", "/v1/notes/e:zzz/done", {})[0], 404)

    def test_07_readings(self):
        code, body, _ = req(self.port, "POST", "/v1/readings", {"url": "https://example.com/a", "why": "周末读"})
        self.assertEqual((code, body["title"], body["status"]), (201, "假标题", "new"))
        code, again, _ = req(self.port, "POST", "/v1/readings", {"url": "https://example.com/a"})
        self.assertEqual((code, again["id"]), (200, body["id"]))              # 去重
        self.assertEqual(req(self.port, "POST", "/v1/readings", {"url": "ftp://x"})[0], 400)
        rid = "r:" + body["id"].removeprefix("link-")
        home = json.load(open(dibao.HOME_JSON))
        self.assertEqual([r["rid"] for r in home["readings"]], [rid])
        code, out, _ = req(self.port, "PATCH", f"/v1/readings/{rid}", {"status": "done"})
        self.assertEqual((code, out["status"]), (200, "done"))
        self.assertEqual(json.load(open(dibao.HOME_JSON))["readings"], [])      # done 不再展示
        self.assertEqual(req(self.port, "PATCH", "/v1/readings/r:none", {"status": "done"})[0], 404)
        self.assertEqual(req(self.port, "PATCH", f"/v1/readings/{rid}", {"status": "weird"})[0], 400)

    def test_08_collect_placeholder_host_and_404(self):
        self.assertEqual(req(self.port, "POST", "/v1/collect", {})[:2], (501, {"error": "WP-09b"}))
        self.assertEqual(req(self.port, "GET", "/nope")[0], 404)
        code, _, _ = req(self.port, "GET", "/health", headers={"Host": "evil.example.com"})
        self.assertEqual(code, 421)

    def test_09_cli(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--port", str(self.port), "edits", "--json",
                           json.dumps({"id": "e:cli", "target": "p:test", "fields": {"next": "c"}, "from": "claude"})])
        self.assertEqual(rc, 0)
        self.assertIn("HTTP 200", buf.getvalue())
        self.assertIn('"project"', buf.getvalue())


class SyncTest(unittest.TestCase):
    """GitHub 拉回：另一个克隆（模拟网页 / iOS）往远端追加 pending 行，服务一轮 sync_cycle 就落卡。"""

    def test_sync_down_applies_remote_pending(self):
        sh = _env.sh
        sh("git", "init", "-q", "--bare", "-b", "home-data", _env.REMOTE)
        sh("git", "-C", _env.DATA, "remote", "add", "origin", _env.REMOTE)
        sh("git", "-C", _env.DATA, "push", "-q", "origin", "HEAD:home-data")
        other = os.path.join(_env.ROOT, "other")
        sh("git", "clone", "-q", "-b", "home-data", _env.REMOTE, other)
        sh("git", "-C", other, "config", "user.email", "o@example.com")
        sh("git", "-C", other, "config", "user.name", "o")
        # 本地先有一次未推送的账本追加；远端同时在同一文件末尾追加 → union 合并不冲突
        svc = dibao.Service(_cfg(), push=False)
        svc.prepare_repo()
        with dibao.data_lock():
            ledger.append(dibao.READINGS, {"id": "link-20261028-aaaa", "title": "本地", "url": "https://l", "status": "new", "added": "2026-10-28", "ts": "t"})
            svc._commit(["readings.jsonl"], "local reading")
        with open(os.path.join(other, "readings.jsonl"), "a") as f:
            f.write(json.dumps({"id": "link-20261028-bbbb", "title": "远端", "url": "https://r", "status": "new", "added": "2026-10-28", "ts": "t"}) + "\n")
        with open(os.path.join(other, "edits.jsonl"), "a") as f:
            f.write(json.dumps({"id": "e:web1", "ts": "2026-10-28T00:00:00Z", "target": "p:test", "fields": {"next": "from web"}, "state": "pending", "from": "web"}) + "\n")
        sh("git", "-C", other, "add", ".")
        sh("git", "-C", other, "commit", "-q", "-m", "web write")
        sh("git", "-C", other, "push", "-q", "origin", "HEAD:home-data")

        svc.sync_cycle()

        self.assertEqual(ledger.read_ledger(dibao.EDITS)["e:web1"]["state"], "applied")
        home = json.load(open(dibao.HOME_JSON))
        self.assertEqual(home["projects"][0]["next"], "from web")
        self.assertEqual({r["title"] for r in home["readings"]}, {"本地", "远端"})
        self.assertIsNotNone(svc.last_sync_down)
        self.assertEqual(sh("git", "-C", _env.DATA, "status", "--porcelain"), "")
        self.assertEqual(sh("git", "-C", _env.DATA, "rev-list", "--count", "origin/home-data..HEAD").strip() != "0", True)

        svc.do_push()                                                      # 防抖线程干的事：推回 GitHub
        self.assertEqual(sh("git", "-C", _env.DATA, "rev-list", "--count", "origin/home-data..HEAD").strip(), "0")
        self.assertIsNotNone(svc.last_push)


if __name__ == "__main__":
    unittest.main()
