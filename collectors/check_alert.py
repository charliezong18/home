#!/usr/bin/env python3
"""验收用：在临时缓存目录里让 menxia 连败 5 次，确认只在第 3 次「推送」一次（dry-run，不真推），成功后清零。"""
import json
import os
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp(prefix="ai-home-test-")
env = {**os.environ, "AI_HOME_CACHE": tmp, "AI_HOME_NTFY_DRYRUN": "1"}
alerts = 0
for i in range(1, 6):
    r = subprocess.run([sys.executable, os.path.join(here, "status.py"), "menxia", "fail", f"人为失败 {i}"],
                       env=env, capture_output=True, text=True)
    hit = "[dry-run]" in r.stdout
    alerts += hit
    print(f"fail #{i}: alert={hit}")
subprocess.run([sys.executable, os.path.join(here, "status.py"), "menxia", "ok"], env=env, capture_output=True)
st = json.load(open(os.path.join(tmp, "status.json")))["menxia"]
print("after ok:", json.dumps(st, ensure_ascii=False))
ok = alerts == 1 and st["consecutive_failures"] == 0
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
