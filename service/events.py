"""推送事件与去重（#192 Q12，WP-11）：四类事件 → APNs；APNs 无设备 / 全失败 → ntfy 兜底。

去重状态 = 追加式 jsonl（缺省 ~/.local/state/dibao/pushed.jsonl，DIBAO_STATE_DIR 可改）。
读取规则：带日期的键（fail-/due-）只看最近 7 天；fold-/note-/baseline- 的 id 永不复用，永久保留。
"""
import json
import logging
import os
import subprocess
import threading
import time
from urllib.parse import quote

import apns as apns_mod

log = logging.getLogger("dibao.events")
DEDUP_DAYS = 7
PERMANENT = ("fold-", "note-", "baseline-")
FAIL_AFTER = 3


def state_path():
    d = os.environ.get("DIBAO_STATE_DIR") or os.path.expanduser("~/.local/state/dibao")
    return os.path.join(d, "pushed.jsonl")


def default_ntfy(text, url):
    """沿用 dibao_sched.notify 的 ntfy 渠道与 AI_HOME_NTFY_DRYRUN 开关；Click 头带 url。"""
    from common import NTFY_URL
    if os.environ.get("AI_HOME_NTFY_DRYRUN") == "1":
        log.warning("[dry-run] 本应推送 ntfy：%s %s", text, url)
        return True
    cmd = ["curl", "-sS", "-m", "15", "-H", "Title: AI home dibao"]
    if url:
        cmd += ["-H", f"Click: {url}"]
    try:
        r = subprocess.run(cmd + ["-d", text, NTFY_URL], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("ntfy 推送失败：%s", e)
        return False
    return r.returncode == 0


def home_url(item_id):
    return "menxia://home?id=" + quote(str(item_id), safe=":")


class Notifier:
    def __init__(self, apns, devices_path, ntfy=default_ntfy, on_deactivate=None, clock=time.time):
        self.apns, self.devices_path, self.ntfy, self.on_deactivate, self.clock = apns, devices_path, ntfy, on_deactivate, clock
        self.lock = threading.Lock()
        self.last_push = self.last_error = None
        self.pushed = self._load()

    # ---------- 去重状态 ----------
    def _load(self):
        cutoff, keys = self.clock() - DEDUP_DAYS * 86400, set()
        try:
            with open(state_path(), encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("key") and (r["key"].startswith(PERMANENT) or r.get("t", 0) >= cutoff):
                        keys.add(r["key"])
        except OSError:
            pass
        return keys

    def _record(self, key, **extra):
        self.pushed.add(key)
        os.makedirs(os.path.dirname(state_path()), exist_ok=True)
        with open(state_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, "t": int(self.clock()), **extra}, ensure_ascii=False) + "\n")

    # ---------- 发送 ----------
    def devices(self):
        return apns_mod.active_devices(self.devices_path) if self.apns.configured else []

    def _deliver(self, key, title, body, url, allow_ntfy):
        """返回 'apns' / 'ntfy' / 'failed' / 'none'（无设备且不许 ntfy）。成功才算送达。"""
        payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default", "thread-id": "dibao"}, "url": url}
        ok = 0
        for d in self.devices():
            status, reason = self.apns.send(d, payload, key)
            if status == 200:
                ok += 1
            else:
                self.last_error = f"{status} {reason}"[:200]
                if apns_mod.Apns.is_dead(status, reason) and self.on_deactivate:
                    self.on_deactivate(d["token"], reason or str(status))
        if ok:
            self.last_push = self.clock()
            return "apns"
        if not allow_ntfy:
            return "failed" if self.devices() else "none"
        if self.ntfy(f"{title}：{body}", url):
            self.last_push = self.clock()
            return "ntfy"
        self.last_error = "ntfy 失败"
        return "failed"

    def push(self, key, title, body, url, allow_ntfy=True):
        """去重 + 送达；只有送达才记入已推集合（失败下次扫描还会重试）。返回 'dup' 或 _deliver 的结果。"""
        with self.lock:
            if key in self.pushed:
                return "dup"
            res = self._deliver(key, title, body, url, allow_ntfy)
            if res in ("apns", "ntfy"):
                self._record(key, via=res)
            return res

    def send_test(self, title, body, url):
        with self.lock:
            return self._deliver(f"test-{int(self.clock())}", title, body, url, True)

    # ---------- 四类事件 ----------
    def scan_home(self, home):
        """采集 / 重算之后：新「你拍」折 + 采集连续失败。"""
        out = []
        folds = ((home.get("folders") or {}).get("oldest") or {}).get("你拍") or []
        if "baseline-folds" not in self.pushed:      # 首次部署：现有的折不算新，静默记下
            for f in folds:
                self._record(f"fold-{f['n']}", via="baseline")
            self._record("baseline-folds")
        else:
            for f in folds:
                out.append(self.push(f"fold-{f['n']}", "待你拍", f"#{f['n']} {f.get('title', '')}", f"menxia://?pr={f['n']}"))
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        for src, s in (home.get("sources") or {}).items():
            if int(s.get("consecutive_failures") or 0) >= FAIL_AFTER:
                # status.py 已经在 ≥3 次时自己推 ntfy，这里只走 APNs，避免重复提醒
                out.append(self.push(f"fail-{src}-{today}", "邸报", f"邸报采集 {src} 连续失败", "menxia://home", allow_ntfy=False))
        return out

    def note_done(self, eid, title, target):
        return self.push(f"note-{eid}", "邸报", f"附言已处理：{title or target}", home_url(target))

    def scan_due(self, home, today):
        """每天 07:30 后跑一次：projects[].deadline 与 due[].date 等于今天的项。"""
        items = [(p.get("id"), p.get("title"), p.get("deadline")) for p in home.get("projects") or []]
        items += [(d.get("id"), d.get("title"), d.get("date")) for d in home.get("due") or []]
        return [self.push(f"due-{i}-{today}", "邸报", f"今天截止：{t}", home_url(i))
                for i, t, date in items if i and date == today]

    def health(self):
        iso = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None  # noqa: E731
        return {"configured": self.apns.configured, "devices": len(self.devices()),
                "last_push": iso(self.last_push), "last_error": self.last_error}
