#!/usr/bin/env python3
"""记录某源本轮成败，必要时推 ntfy。

  status.py <source> ok
  status.py <source> fail <错误信息>

本机权威副本在 ~/Library/Caches/ai-home/status.json；build_home 每轮把它抄进 home-data 分支。
同源连续失败 ≥3 次且今天还没告过警 → notify.send 推一条（每源每天最多一条，恢复不发）。
AI_HOME_NTFY_DRYRUN=1 时只打印不推送（测试用）。
"""
import os
import sys

import notify
from common import CACHE_DIR, ensure_dirs, log, now_iso, read_json, today_local, write_json

STATUS = os.path.join(CACHE_DIR, "status.json")
ALERT_AFTER = 3


def main():
    if len(sys.argv) < 3 or sys.argv[2] not in ("ok", "fail"):
        print(__doc__)
        return 2
    source, result = sys.argv[1], sys.argv[2]
    msg = " ".join(sys.argv[3:]).strip()[:300]
    ensure_dirs()
    allst = read_json(STATUS, {}) or {}
    st = allst.setdefault(source, {"last_ok": None, "last_error": None, "last_error_msg": "",
                                   "consecutive_failures": 0, "last_alert_date": None})
    if result == "ok":
        st["last_ok"] = now_iso()
        st["consecutive_failures"] = 0
    else:
        st["last_error"] = now_iso()
        st["last_error_msg"] = msg
        st["consecutive_failures"] = int(st.get("consecutive_failures") or 0) + 1
        if st["consecutive_failures"] >= ALERT_AFTER and st.get("last_alert_date") != today_local():
            text = f"AI 主页：{source} 连续失败 {st['consecutive_failures']} 次。最近一次：{msg or '无信息'}"
            if os.environ.get("AI_HOME_NTFY_DRYRUN") == "1":
                log(f"[dry-run] 本应推送 ntfy：{text}")
                st["last_alert_date"] = today_local()
            elif notify.send(text, title="AI home collector"):
                st["last_alert_date"] = today_local()
                log(f"ntfy 已推送：{text}")
            else:
                log("ntfy 推送失败（详见 notify 日志）")
    write_json(STATUS, allst)
    log(f"status {source} {result} consecutive_failures={st['consecutive_failures']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
