# AI 主页 Phase 1 进度（任务 t-20261013-220403-1aa7）

## 已完成
- [x] 数据分支：`review` 孤儿分支 `home-data`（worktree `~/Developer/review-home-data`），README/config.yaml/readings.jsonl 已推
- [x] 采集器：common / collect_menxia / collect_dashboard / add_link / status / build_home / run_all.sh / verify_counts
- [x] 首轮 run_all：home.json 已推，计数与 gh pr list 一致（35 open：你拍 9 / 你读 6 / 闲 20，读物 22）
- [x] 页面 index.html / app.js / style.css
- [x] launchd plist 源文件 `launchd/com.charlie.ai-home.plist`

## 下一步
- [ ] 建公开仓 charliezong18/home、push、开 Pages、curl 验证
- [ ] 第二轮 run_all 验证「内容不变不提交」
- [ ] add_link 实测一条
- [ ] plist 拷进 ~/Library/LaunchAgents、bootstrap、kickstart、看日志
- [ ] 报告
