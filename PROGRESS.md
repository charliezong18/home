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

## 编辑流（任务 t-20261013-231813-3842，2026-10-13 23:40 PDT）
- [x] 写入探测：gh 账号 token 对 home-data PUT/DELETE `edits/.probe` 成功；**页面 PAT 本身未探测**（localStorage 读不到）
- [x] `collectors/apply_edits.py`（apply / --list-pending-notes / --mark-done）+ build_home 写 `pending_edits`/`hermes_edits`
- [x] 验收：测试编辑 → needs_hermes → list 列出 → mark-done 后不列；vault 卡已用备份原样恢复（diff 空）
- [x] launchd `com.charlie.ai-home-edits`（300s，run_edits.sh）已 bootstrap
- [x] 页面：弹窗「编辑 → 保存」、乐观更新、● 标记；已 push main
- [x] README（home + home-data）编辑流一节
- [ ] Charlie 浏览器实测「编辑 → 保存」（含 PAT 是否有 Contents: write）
- [ ] Hermes cron：每 15 分钟 `apply_edits.py --list-pending-notes`，有单就派 Claude，完了 `--mark-done`

## dibao A 期（#192 / #187）
- [x] WP-09a 服务核心（已合并）
- [x] WP-09b 采集调度 + push 防抖 + `/v1/collect` + cli 子命令 + 旧 plist 进 `launchd/_retired/` + Makefile `retire`/`rollback`/`status`（代码就绪，**未执行 retire**）
- [ ] Hermes：`make install` → `make retire` → 改两条 cron（见 README「服务化切换步骤」）

## 下一步（留给 Charlie / 后续）
- [ ] 四端打开 + 同源 token 是否读到（§7 第 5 行）——需 Charlie 在 Mac / iPhone / iPad / 工作电脑上各开一次
- [ ] Hermes 路由：「只有链接（+一句话）」→ 调 `python3 ~/Developer/home/collectors/add_link.py <url> [附言]`，成功回 ✓
- [ ] 真实「采集器 #1 连败 3 次」端到端（页面变灰 + ntfy 一条）未做，只做了 dry-run
- [ ] Phase 2 / 3
