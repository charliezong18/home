#!/bin/bash
# AI 主页一轮采集：各采集器 → 记 status → 汇总推送。launchd 每 30 分钟跑一次。
# 单个采集器失败不拦后面的；汇总照样跑（页面会用旧快照 + 灰角标）。
# 服务化后由 dibao 调度（service/sched.py）；手动跑也安全（拿同一把 data_lock）。
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
cd "$DIR" || exit 1

echo "=== run_all $(date '+%Y-%m-%d %H:%M:%S') ==="

run_source() {  # $1 = 源名, $2 = 脚本
  local out rc
  out="$("$PY" "$DIR/$2" 2>&1)"; rc=$?
  echo "$out"
  if [ $rc -eq 0 ]; then
    "$PY" "$DIR/status.py" "$1" ok
  else
    "$PY" "$DIR/status.py" "$1" fail "$(echo "$out" | tail -1)"
  fi
}

run_source menxia collect_menxia.py
run_source dashboard collect_dashboard.py
run_source countdown collect_countdown.py
run_source backlog collect_backlog.py
run_source gtasks collect_gtasks.py

out="$("$PY" "$DIR/build_home.py" 2>&1)"; rc=$?
echo "$out"
case $rc in
  0) "$PY" "$DIR/status.py" readings ok; "$PY" "$DIR/status.py" build ok ;;
  3) "$PY" "$DIR/status.py" readings fail "readings.jsonl 解析失败"; "$PY" "$DIR/status.py" build ok ;;
  *) "$PY" "$DIR/status.py" build fail "$(echo "$out" | tail -1)" ;;
esac
echo "=== done $(date '+%H:%M:%S') ==="
