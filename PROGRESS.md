# AI 主页 Phase 1 进度（任务 t-20261013-220403-1aa7）

## 已完成（2026-10-13 22:13 PDT）
- [x] 数据分支：`review` 孤儿分支 `home-data`（worktree `~/Developer/review-home-data`），README(schema)/config.yaml/readings.jsonl/home.json/status.json 已推
- [x] 采集器：common / collect_menxia / collect_dashboard / add_link / status / build_home / run_all.sh
- [x] 验收脚本：verify_counts.py（计数对照）、check_pages.py（Pages 轮询）、test_alert.py（告警 dry-run）
- [x] run_all 真跑：home.json 推上并经 contents API 读回；计数与 gh pr list 一致（35 open：你拍 9 / 你读 6 / agent 0 / 闲 20，读物 22）
- [x] 内容不变不提交（第三轮起「内容未变，不提交」）
- [x] add_link 实测（example.com → readings.jsonl → home.json readings:1），测试条目已追加 dropped 作废
- [x] 公开仓 charliezong18/home + Pages：https://charliezong18.github.io/home/ 200，title 命中
- [x] launchd com.charlie.ai-home 已 bootstrap，RunAtLoad 与 kickstart 两轮 exit 0
- [x] 告警逻辑 dry-run：连败 5 次只告警 1 次，成功清零

## 下一步（留给 Charlie / 后续）
- [ ] 四端打开 + 同源 token 是否读到（§7 第 5 行）——需 Charlie 在 Mac / iPhone / iPad / 工作电脑上各开一次
- [ ] Hermes 路由：「只有链接（+一句话）」→ 调 `python3 ~/Developer/home/collectors/add_link.py <url> [附言]`，成功回 ✓
- [ ] 真实「采集器 #1 连败 3 次」端到端（页面变灰 + ntfy 一条）未做，只做了 dry-run
- [ ] Phase 2 / 3
