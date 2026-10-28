#!/usr/bin/env python3
"""丢链接 CLI：add_link.py <url> [note]

抓网页标题（10 秒超时，失败用 url 当标题），追加一行到 home-data 分支的 readings.jsonl，
commit + push。只打印一行结果（Hermes 据此回 ✓ 或失败原因）。退出码 0 = 成功。
"""
import hashlib
import html
import json
import os
import re
import sys
import urllib.request

from common import DATA_DIR, commit_and_push, data_lock, now_iso, sync_down, today_local

LEDGER = os.path.join(DATA_DIR, "readings.jsonl")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Safari/605.1.15"


def fetch_title(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(512 * 1024)
            charset = resp.headers.get_content_charset()
        if not charset:
            m = re.search(rb'charset=["\']?([\w-]+)', raw[:4096], re.I)
            charset = m.group(1).decode() if m else "utf-8"
        text = raw.decode(charset, errors="replace")
        for pat in (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                    r"<title[^>]*>(.*?)</title>"):
            m = re.search(pat, text, re.I | re.S)
            if m and m.group(1).strip():
                return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()[:200], True
    except Exception:  # noqa: BLE001 — 抓不到标题不算失败
        pass
    return url, False


def build_entry(url, note, title):
    """readings.jsonl 一行（CLI 与 dibao 服务共用）。"""
    return {
        "id": f"link-{today_local().replace('-', '')}-{hashlib.sha1(url.encode()).hexdigest()[:4]}",
        "title": title,
        "url": url,
        "source": "link",
        "added": today_local(),
        "why": note,
        "status": "new",
        "ts": now_iso(),
    }


def main():
    if len(sys.argv) < 2 or not re.match(r"^https?://", sys.argv[1]):
        print("FAIL 用法：add_link.py <http(s) url> [附言]")
        return 2
    url = sys.argv[1].strip()
    note = " ".join(sys.argv[2:]).strip()
    title, got = fetch_title(url)
    entry = build_entry(url, note, title)
    try:
        with data_lock():
            sync_down()
            if os.path.exists(LEDGER):
                with open(LEDGER, encoding="utf-8") as f:
                    for line in f:
                        try:
                            if json.loads(line).get("url") == url:
                                print(f"OK 已在读物里：{title}")
                                return 0
                        except ValueError:
                            continue
            with open(LEDGER, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            commit_and_push(["readings.jsonl"], f"readings: + {title[:60]}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL 写账本/推送失败：{str(e)[:200]}")
        return 1
    print(f"OK {title}" + ("" if got else "（没抓到标题，先用链接代替）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
