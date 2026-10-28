"""APNs 发送端（#192 Q12，WP-11）：标准库 + `openssl`（ES256 签名）+ `curl --http2`（HTTP/2）子进程。

Python 标准库既没有 HTTP/2 也没有 ES256，所以：JWT 由 `openssl dgst -sha256 -sign` 出 DER 签名再转 raw r||s；
请求由 `curl --http2` 发。本模块不碰 git、不碰锁：停用设备通过 on_deactivate 回调交给调用方落账本。
日志里 device token 只记末 6 位，JWT 不记。
"""
import base64
import hashlib
import json
import logging
import os
import subprocess
import time

log = logging.getLogger("dibao.apns")

HOSTS = {"production": "api.push.apple.com", "sandbox": "api.sandbox.push.apple.com"}
JWT_TTL = 50 * 60                      # Apple 要求 20–60 分钟内刷新
DEFAULT_TEAM = "Q8XV65MMXK"
DEFAULT_BUNDLE = "com.charliezong.menxia"
BAD_PROVIDER = ("InvalidProviderToken", "ExpiredProviderToken")


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def der_to_raw(der):
    """ECDSA-Sig-Value（SEQUENCE{INTEGER r, INTEGER s}）→ 64 字节 r||s。"""
    if len(der) < 8 or der[0] != 0x30:
        raise ValueError("不是 DER 签名")
    i = 2 + (der[1] & 0x7F if der[1] & 0x80 else 0)
    parts = []
    for _ in range(2):
        if i + 2 > len(der) or der[i] != 0x02:
            raise ValueError("DER 整数头不对")
        n = der[i + 1]
        v = der[i + 2:i + 2 + n].lstrip(b"\x00")
        if len(v) > 32:
            raise ValueError("DER 整数超过 32 字节")
        parts.append(v.rjust(32, b"\x00"))
        i += 2 + n
    return b"".join(parts)


def default_key_path(key_id):
    return os.environ.get("APNS_KEY_PATH") or os.path.expanduser(f"~/.config/dibao/apns/AuthKey_{key_id}.p8")


def make_jwt(key_id, team_id, key_path=None, now=None, runner=subprocess.run):
    key_path = os.path.expanduser(key_path) if key_path else default_key_path(key_id)
    head = b64u(json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"iss": team_id, "iat": int(now if now is not None else time.time())},
                             separators=(",", ":")).encode())
    signing_input = f"{head}.{claims}".encode()
    r = runner(["openssl", "dgst", "-sha256", "-sign", key_path], input=signing_input, capture_output=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"openssl 签名失败 rc={r.returncode}: {(r.stderr or b'').decode(errors='replace').strip()[:200]}")
    return f"{head}.{claims}.{b64u(der_to_raw(r.stdout))}"


def collapse_id(key):
    """apns-collapse-id ≤64 字节；含非 ASCII（项目 id 常含中文）或过长时退成哈希。"""
    if key.isascii() and len(key) <= 64:
        return key
    return "h-" + hashlib.sha1(key.encode()).hexdigest()[:40]


def tail(token):
    return "…" + token[-6:]


def load_devices(path):
    """devices.jsonl（同 token 后者生效）→ 全部设备 dict 列表。坏行跳过。"""
    latest = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("token"):
                    latest[d["token"]] = {**latest.get(d["token"], {}), **d}
    except OSError:
        pass
    return list(latest.values())


def active_devices(path):
    return [d for d in load_devices(path) if d.get("active", True) is not False]


class Apns:
    """cfg = config.toml 的 [apns] 段（dict）。runner 可注入（测试用假 curl / openssl）。"""

    def __init__(self, cfg=None, runner=subprocess.run, clock=time.time):
        cfg = cfg or {}
        self.key_id = cfg.get("key_id", "")
        self.team_id = cfg.get("team_id", DEFAULT_TEAM)
        self.bundle_id = cfg.get("bundle_id", DEFAULT_BUNDLE)
        self.key_path = os.path.expanduser(cfg.get("key_path") or "") or (default_key_path(self.key_id) if self.key_id else "")
        self.curl = cfg.get("curl", "curl")
        self.runner, self.clock = runner, clock
        self._jwt, self._jwt_at = None, 0.0

    @property
    def configured(self):
        return bool(self.key_id and self.key_path and os.path.exists(self.key_path))

    def jwt(self, force=False):
        if force or not self._jwt or self.clock() - self._jwt_at > JWT_TTL:
            self._jwt = make_jwt(self.key_id, self.team_id, self.key_path, self.clock(), self.runner)
            self._jwt_at = self.clock()
        return self._jwt

    def curl_argv(self, token, env, jwt, collapse):
        host = HOSTS["sandbox" if env == "sandbox" else "production"]
        return [self.curl, "--http2", "-sS", "-m", "20", "-w", "\n%{http_code}",
                "-H", f"authorization: bearer {jwt}", "-H", f"apns-topic: {self.bundle_id}",
                "-H", "apns-push-type: alert", "-H", "apns-priority: 10", "-H", f"apns-collapse-id: {collapse}",
                "--data-binary", "@-", f"https://{host}/3/device/{token}"]

    def _once(self, token, env, body, collapse, force_jwt=False):
        argv = self.curl_argv(token, env, self.jwt(force_jwt), collapse)
        r = self.runner(argv, input=body, capture_output=True, timeout=30)
        out = (r.stdout or b"").decode(errors="replace")
        if r.returncode != 0:
            return 0, f"curl rc={r.returncode}: {(r.stderr or b'').decode(errors='replace').strip()[:200]}"
        text, _, code = out.rpartition("\n")
        try:
            reason = (json.loads(text) or {}).get("reason", "") if text.strip() else ""
        except ValueError:
            reason = text.strip()[:200]
        return int(code) if code.strip().isdigit() else 0, reason

    def send(self, device, payload, collapse_key):
        """发一台设备。返回 (http 状态, reason)；状态 0 = 本机失败（签名 / curl）。403 令牌问题刷新 JWT 重试一次。"""
        token, env = device["token"], device.get("env", "production")
        body, collapse = json.dumps(payload, ensure_ascii=False).encode(), collapse_id(collapse_key)
        try:
            status, reason = self._once(token, env, body, collapse)
            if status == 403 and reason in BAD_PROVIDER:
                status, reason = self._once(token, env, body, collapse, force_jwt=True)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            status, reason = 0, str(e)[:200]
        log.info("apns %s %s → %s %s", env, tail(token), status, reason)
        return status, reason

    @staticmethod
    def is_dead(status, reason):
        return status == 410 or (status == 400 and reason == "BadDeviceToken")
