# AI 主页（home）

一页看完「要读的」和「在跟的」：门下折务、弘文馆、Dashboard 项目卡、随手丢的链接。
设计方案：门下 review #179（2026-10-14 画可），本仓实现 Phase 1。

- 页面：<https://charliezong18.github.io/home/>（手机 Safari → 分享 → 添加到主屏幕）
- 本仓（公开）：**只有页面代码与采集器代码，不含任何数据**
- 数据：私有仓 `charliezong18/review` 的孤儿分支 `home-data`（不进 main、不开 PR，门下看不到）

## 架构

```
Mac mini launchd (com.charlie.ai-home, 每 30 分钟)
  └─ collectors/run_all.sh
       ├─ collect_menxia.py     gh api → review 仓 open PR + labels   ─┐
       ├─ collect_dashboard.py  vault Dashboard/*.md（30s 硬超时）     ├→ ~/Library/Caches/ai-home/snapshot-*.json
       ├─ status.py             每源成败 → ~/Library/Caches/ai-home/status.json（连败 3 次推 ntfy，每源每天 ≤1 条）
       └─ build_home.py         快照 + readings.jsonl + status → home.json / status.json → commit + push（不变不提交）
                                                         ↓
                          review 仓 home-data 分支（worktree ~/Developer/review-home-data）
                                                         ↓  contents API + 门下 token（localStorage `zhupi.token`）
                          浏览器 charliezong18.github.io/home/（与门下同源，token 共用）

Hermes 丢链接 → collectors/add_link.py <url> [附言] → readings.jsonl 追加一行 → commit + push
```

- 采集失败时页面继续用上一份快照，对应栏的「数据更新于」角标变灰并带 ⚠，hover 看错误。
- 健康时也每 3 小时提交一次心跳，所以「数据更新于」最多约 3.5 小时前；超过 4 小时即判过期变灰。
- 页面无构建、无第三方脚本/字体，CSP 只放行 `api.github.com`；所有数据字符串都以 textContent 渲染。

## Schema

`home.json` / `readings.jsonl` / `status.json` 的完整 schema 写在数据分支的 README：
`gh api repos/charliezong18/review/contents/README.md?ref=home-data -H 'Accept: application/vnd.github.raw'`
（本地：`~/Developer/review-home-data/README.md`）。概要：

| 键 | 内容 |
|---|---|
| `sources.{menxia,dashboard,readings}` | `updated / ok / consecutive_failures / last_error` |
| `folders.counts` | 按 `wait:` label 计：你拍 / 你读 / agent / 闲 / 未标注 |
| `folders.oldest` | 每类最旧 8 折，带门下深链 `https://charliezong18.github.io/menxia/?pr=<n>` |
| `library` | 弘文馆 = open 且 `kind:读物` |
| `projects` | Dashboard 卡：frontmatter + `## 现状` 首段 + 里程碑勾选比例 |
| `readings` | `readings.jsonl` 按 id 归并、去掉 done/dropped；超 30 天 `folded` |

## 丢链接 CLI（给 Hermes 调）

```bash
python3 ~/Developer/home/collectors/add_link.py 'https://example.com/post' '周末读'
# → 打印一行：OK <标题>  /  OK 已在读物里：<标题>  /  FAIL <原因>；退出码 0 = 成功
```

标题抓取 10 秒超时，失败用 URL 当标题（仍算成功）。同一 URL 不重复记。页面只读 `home.json`，所以链接要等下一轮 build 收录后才在页面出现——最长 30 分钟（急的话手动 `launchctl kickstart`）。

## 手动跑

```bash
bash ~/Developer/home/collectors/run_all.sh              # 跑一轮
python3 ~/Developer/home/collectors/verify_counts.py     # 对照 gh pr list 手算计数 vs 线上 home.json
launchctl kickstart gui/$(id -u)/com.charlie.ai-home     # 让 launchd 立刻跑一轮
tail -50 ~/Library/Logs/ai-home/run_all.out.log
cat ~/Library/Caches/ai-home/status.json
AI_HOME_NTFY_DRYRUN=1 python3 ~/Developer/home/collectors/status.py menxia fail 测试   # 告警逻辑演练，不真推
```

## 安装 / 换机

```bash
git clone https://github.com/charliezong18/home ~/Developer/home
git -C ~/Developer/review fetch origin home-data
git -C ~/Developer/review worktree add ~/Developer/review-home-data home-data
mkdir -p ~/Library/Logs/ai-home
cp ~/Developer/home/launchd/com.charlie.ai-home.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.charlie.ai-home.plist
```

需要：`gh` 已登录且能读 review 仓；`git` 能 push review 仓。

## 卸载

```bash
launchctl bootout gui/$(id -u)/com.charlie.ai-home
rm ~/Library/LaunchAgents/com.charlie.ai-home.plist
git -C ~/Developer/review worktree remove ~/Developer/review-home-data
# 可选：删远端数据分支 git -C ~/Developer/review push origin --delete home-data
# 可选：gh repo delete charliezong18/home；rm -r ~/Library/Caches/ai-home ~/Library/Logs/ai-home
```

## 加 RSS 源（Phase 3 占位）

尚未实现。计划：在 `home-data` 分支 `config.yaml` 的 `rss:` 下加 `- { name, url }`，由将来的
`collectors/collect_rss.py` 每天两次读最近 48 小时标题，以 `source: "rss"` 追加进 `readings.jsonl`。
Phase 2（Backlog / 倒数日 / 日历 / 账单快递，账单不存金额）同样尚未实现，页面上已留占位。
