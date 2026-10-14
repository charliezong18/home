#!/usr/bin/env python3
"""验收用：轮询 Pages 直到 200 且 body 含 <title>AI 主页</title>（最多 10 分钟）。只打印状态。"""
import sys
import time
import urllib.error
import urllib.request

URL = "https://charliezong18.github.io/home/"
deadline = time.time() + 600
while True:
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, headers={"Cache-Control": "no-cache"}), timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
            has = "<title>AI 主页</title>" in body
            print(f"{time.strftime('%H:%M:%S')} HTTP {r.status} title_ok={has} bytes={len(body)} "
                  f"app.js_ref={'app.js' in body}")
            if r.status == 200 and has:
                sys.exit(0)
    except urllib.error.HTTPError as e:
        print(f"{time.strftime('%H:%M:%S')} HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        print(f"{time.strftime('%H:%M:%S')} ERR {e}")
    if time.time() > deadline:
        sys.exit(1)
    time.sleep(20)
