import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402
import apns  # noqa: E402
import config  # noqa: E402
import dibao  # noqa: E402


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def raw_to_der(raw):
    """测试用反向转换：r||s → DER，让 openssl 能验。"""
    def der_int(b):
        b = b.lstrip(b"\x00") or b"\x00"
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + bytes([len(b)]) + b
    body = der_int(raw[:32]) + der_int(raw[32:])
    return b"\x30" + bytes([len(body)]) + body


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, check=True)


class FakeRunner:
    """openssl 走真的；curl 记录 argv / stdin，按脚本回包。"""

    def __init__(self, curl_replies):
        self.curl_replies, self.curls, self.openssl_calls = list(curl_replies), [], 0

    def __call__(self, argv, **kw):
        if argv[0] == "openssl":
            self.openssl_calls += 1
            return subprocess.run(argv, **kw)
        self.curls.append((argv, kw.get("input")))
        code, body = self.curl_replies.pop(0)
        return subprocess.CompletedProcess(argv, 0, stdout=(body + "\n" + str(code)).encode(), stderr=b"")


class ApnsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wp11-")
        ec, cls.p8, cls.pub = (os.path.join(cls.tmp, n) for n in ("ec.pem", "AuthKey_TESTKEY.p8", "pub.pem"))
        sh("openssl", "ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", ec)
        sh("openssl", "pkcs8", "-topk8", "-nocrypt", "-in", ec, "-out", cls.p8)
        sh("openssl", "pkey", "-pubout", "-in", cls.p8, "-out", cls.pub)

    def apns(self, runner, clock=lambda: 1_000_000):
        return apns.Apns({"key_id": "TESTKEY", "key_path": self.p8}, runner=runner, clock=clock)

    def test_jwt_shape_and_signature_verifies(self):
        jwt = apns.make_jwt("TESTKEY", "Q8XV65MMXK", self.p8, now=1_700_000_000)
        head, claims, sig = jwt.split(".")
        self.assertNotIn("=", jwt)
        self.assertEqual(json.loads(b64d(head)), {"alg": "ES256", "kid": "TESTKEY"})
        self.assertEqual(json.loads(b64d(claims)), {"iss": "Q8XV65MMXK", "iat": 1_700_000_000})
        raw = b64d(sig)
        self.assertEqual(len(raw), 64)
        data, sigf = os.path.join(self.tmp, "data"), os.path.join(self.tmp, "sig.der")
        with open(data, "wb") as f:
            f.write(f"{head}.{claims}".encode())
        with open(sigf, "wb") as f:
            f.write(raw_to_der(raw))
        out = sh("openssl", "dgst", "-sha256", "-verify", self.pub, "-signature", sigf, data).stdout
        self.assertIn(b"Verified OK", out)

    def test_der_to_raw_pads_and_strips(self):
        r, s = b"\x00" + b"\xff" * 32, b"\x01\x02"        # r 带前导 0（高位为 1），s 很短
        der = b"\x30" + bytes([4 + len(r) + len(s)]) + b"\x02" + bytes([len(r)]) + r + b"\x02" + bytes([len(s)]) + s
        raw = apns.der_to_raw(der)
        self.assertEqual(raw, b"\xff" * 32 + b"\x00" * 30 + b"\x01\x02")
        self.assertEqual(apns.der_to_raw(raw_to_der(raw)), raw)
        with self.assertRaises(ValueError):
            apns.der_to_raw(b"garbage-not-der")

    def test_curl_argv_per_env(self):
        run = FakeRunner([(200, ""), (200, "")])
        a = self.apns(run)
        self.assertEqual(a.send({"token": "a" * 64, "env": "sandbox"}, {"aps": {}}, "fold-1"), (200, ""))
        a.send({"token": "b" * 64, "env": "production"}, {"aps": {}}, "fold-2")
        sandbox, prod = run.curls[0][0], run.curls[1][0]
        self.assertEqual(sandbox[0], "curl")
        self.assertIn("--http2", sandbox)
        self.assertEqual(sandbox[-1], "https://api.sandbox.push.apple.com/3/device/" + "a" * 64)
        self.assertEqual(prod[-1], "https://api.push.apple.com/3/device/" + "b" * 64)
        for h in ("apns-topic: com.charliezong.menxia", "apns-push-type: alert", "apns-priority: 10",
                  "apns-collapse-id: fold-1"):
            self.assertIn(h, sandbox)
        self.assertTrue(any(x.startswith("authorization: bearer ") and x.count(".") == 2 for x in sandbox))
        self.assertEqual(json.loads(run.curls[0][1]), {"aps": {}})

    def test_jwt_cached_50_minutes(self):
        now = [1_000_000]
        run = FakeRunner([(200, "")] * 3)
        a = self.apns(run, clock=lambda: now[0])
        dev = {"token": "c" * 64, "env": "sandbox"}
        a.send(dev, {}, "k1")
        now[0] += 49 * 60
        a.send(dev, {}, "k2")
        self.assertEqual(run.openssl_calls, 1)
        now[0] += 2 * 60
        a.send(dev, {}, "k3")
        self.assertEqual(run.openssl_calls, 2)

    def test_403_invalid_provider_token_refreshes_once(self):
        run = FakeRunner([(403, '{"reason":"InvalidProviderToken"}'), (200, "")])
        self.assertEqual(self.apns(run).send({"token": "d" * 64, "env": "sandbox"}, {}, "k"), (200, ""))
        self.assertEqual((len(run.curls), run.openssl_calls), (2, 2))
        run = FakeRunner([(403, '{"reason":"InvalidProviderToken"}')] * 2)      # 只重试一次
        self.assertEqual(self.apns(run).send({"token": "d" * 64}, {}, "k"), (403, "InvalidProviderToken"))
        self.assertEqual(len(run.curls), 2)

    def test_dead_token_detection(self):
        self.assertTrue(apns.Apns.is_dead(410, "Unregistered"))
        self.assertTrue(apns.Apns.is_dead(400, "BadDeviceToken"))
        self.assertFalse(apns.Apns.is_dead(400, "PayloadEmpty"))
        self.assertFalse(apns.Apns.is_dead(200, ""))

    def test_curl_failure_is_status_zero(self):
        def boom(argv, **kw):
            if argv[0] == "openssl":
                return subprocess.run(argv, **kw)
            return subprocess.CompletedProcess(argv, 7, stdout=b"", stderr=b"couldn't connect")
        status, reason = self.apns(boom).send({"token": "e" * 64}, {}, "k")
        self.assertEqual(status, 0)
        self.assertIn("rc=7", reason)

    def test_collapse_id_limits(self):
        self.assertEqual(apns.collapse_id("fold-12"), "fold-12")
        for k in ("due-d:倒数-2026-10-28", "x" * 70):
            c = apns.collapse_id(k)
            self.assertTrue(c.isascii() and len(c) <= 64)

    def test_devices_merge_last_wins(self):
        p = os.path.join(self.tmp, "devices.jsonl")
        with open(p, "w") as f:
            for d in ({"token": "t1", "env": "sandbox", "active": True}, {"token": "t2", "active": True},
                      "not json", {"token": "t1", "active": False}):
                f.write((json.dumps(d) if isinstance(d, dict) else d) + "\n")
        self.assertEqual([d["token"] for d in apns.active_devices(p)], ["t2"])

    def test_service_deactivate_appends_active_false(self):
        cfg = config.load()
        svc = dibao.Service(cfg, push=False)
        svc._deactivate("f" * 64, "Unregistered")
        with open(dibao.DEVICES) as f:
            last = json.loads(f.read().strip().splitlines()[-1])
        self.assertEqual((last["token"], last["active"], last["reason"]), ("f" * 64, False, "Unregistered"))
        self.assertEqual(apns.active_devices(dibao.DEVICES), [])
        self.assertIn("devices: deactivate", _env.sh("git", "-C", _env.DATA, "log", "-1", "--format=%s"))

    def test_health_apns_block(self):
        svc = dibao.Service(config.load(), push=False)
        h = svc.health()["apns"]
        self.assertEqual(set(h), {"configured", "devices", "last_push", "last_error"})
        self.assertFalse(h["configured"])


if __name__ == "__main__":
    unittest.main()
