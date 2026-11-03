"""H2：dibao 本地 API 拒绝浏览器跨站请求（Origin / Sec-Fetch-Site）与非 JSON 的写请求。"""
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402,F401
import cli  # noqa: E402
import config  # noqa: E402
import dibao  # noqa: E402


def raw(port, method, path, data=b"", headers=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data if method != "GET" else None,
                               method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class GuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dibao.fetch_title = lambda url: ("假标题", True)
        cfg = config.load()
        cfg.worker_timeout = 3
        cls.svc = dibao.Service(cfg, push=False)
        cls.srv = dibao.make_server(cls.svc, 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_text_plain_cross_site_post_rejected_without_side_effects(self):
        before = (size(dibao.READINGS), size(dibao.TODO_ACTIONS), size(dibao.EDITS))
        body = b'{"url": "https://attacker.example/x"}'
        self.assertEqual(raw(self.port, "POST", "/v1/readings", body, {"Content-Type": "text/plain"}), 415)
        self.assertEqual(raw(self.port, "POST", "/v1/todos/T1/complete", b"", {"Content-Type": "text/plain"}), 415)
        self.assertEqual(raw(self.port, "POST", "/v1/edits", b'{"target":"p:test","fields":{"next":"x"}}'), 415)
        self.assertEqual(raw(self.port, "PATCH", "/v1/readings/r:x", b'{"status":"done"}',
                             {"Content-Type": "application/x-www-form-urlencoded"}), 415)
        self.assertEqual((size(dibao.READINGS), size(dibao.TODO_ACTIONS), size(dibao.EDITS)), before)

    def test_origin_header_rejected_even_with_json(self):
        before = size(dibao.READINGS)
        h = {"Content-Type": "application/json", "Origin": "https://attacker.example"}
        self.assertEqual(raw(self.port, "POST", "/v1/readings", b'{"url": "https://attacker.example/y"}', h), 403)
        self.assertEqual(raw(self.port, "GET", "/v1/home.json", headers={"Origin": "null"}), 403)
        self.assertEqual(raw(self.port, "POST", "/v1/collect", b"{}",
                             {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(size(dibao.READINGS), before)

    def test_cli_and_plain_get_still_work(self):
        self.assertEqual(raw(self.port, "GET", "/health"), 200)
        self.assertEqual(raw(self.port, "GET", "/health", headers={"Sec-Fetch-Site": "none"}), 200)   # 地址栏直接打开
        code, out = cli.call(self.port, "POST", "/v1/digest/morning", {"dry_run": True})
        self.assertEqual(code, 200, out)
        code, out = cli.call(self.port, "POST", "/v1/readings", {"url": "ftp://bad"})   # JSON 过了闸，业务校验照常 400
        self.assertEqual(code, 400, out)


if __name__ == "__main__":
    unittest.main()
