#!/bin/bash
# 编辑流一轮：主页弹窗写进 edits.jsonl 的 pending → 落 vault 卡（apply_edits 内部会重采 dashboard）→ 汇总推送。
# launchd com.charlie.ai-home-edits 每 5 分钟跑。附言（note）只标 needs_hermes，不在这里处理。
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
cd "$DIR" || exit 1

echo "=== run_edits $(date '+%Y-%m-%d %H:%M:%S') ==="
"$PY" "$DIR/apply_edits.py"; rc=$?
[ $rc -ne 0 ] && echo "apply_edits rc=$rc"
"$PY" "$DIR/build_home.py"
echo "=== done $(date '+%H:%M:%S') ==="
