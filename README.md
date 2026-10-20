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
| `folders.oldest` | 每类最旧 8 折，带门下深链 `https://menxia-reader.github.io/menxia/?pr=<n>` |
| `library` | 弘文馆 = open 且 `kind:读物` |
| `projects` | Dashboard 卡：frontmatter + `## 现状` 首段 + 里程碑勾选比例 |
| `readings` | `readings.jsonl` 按 id 归并、去掉 done/dropped；超 30 天 `folded` |

## 丢链接 CLI（给 Hermes 调）

```bash
python3 ~/Developer/home/collectors/add_link.py 'https://example.com/post' '周末读'
# → 打印一行：OK <标题>  /  OK 已在读物里：<标题>  /  FAIL <原因>；退出码 0 = 成功
```

标题抓取 10 秒超时，失败用 URL 当标题（仍算成功）。同一 URL 不重复记。页面只读 `home.json`，所以链接要等下一轮 build 收录后才在页面出现——最长 30 分钟（急的话手动 `launchctl kickstart`）。

## ID 约定

页面上每条东西点开（或行首/行末的小 ID）都能复制一个短 ID，粘给 Hermes 就知道指哪一条。统一前缀、ASCII 前缀便于语音/复制：

| ID | 指什么 | 来源 |
|---|---|---|
| `#<PR号>` | 折（含弘文馆） | 门下 PR 号，如 `#179` |
| `p:<slug>` | Dashboard 项目卡 | 卡 frontmatter `id:` 优先；否则文件名去 `proj:` 前缀/emoji/标点、小写、空白→`-`（中文保留）；冲突加 `-2`、`-3`。写进 `home.json` 每张卡的 `id` |
| `r:<id>` | 丢的链接 | 账本 id 去掉 `link-` 前缀，如 `r:20261013-b559`；`home.json` 里是 `rid` 字段 |
| `d:<id>` | 到期项（Phase 2） | 只定格式，未实现 |

Hermes 解析（读本地 `~/Developer/review-home-data/home.json`，不走网络；找不到退出码 1）：

```bash
python3 ~/Developer/home/collectors/resolve.py '#179' 'p:门下-menxia' 'r:20261013-b559'
# 每个 ID 一行 JSON：{id, kind, title, url, meta} 或 {id, error:"not found"}
# url：折=门下深链；项目卡=vault 绝对路径；读物=原链接。折只覆盖 home.json 收录的（每类最旧 8 折 + 弘文馆）
```

## 编辑流（弹窗编辑 → 保存 → 触发更新）

1. 页面：项目卡弹窗「编辑」可改 `next` / `status` / `tier` / `deadline`、勾里程碑，另有「给 Hermes 的话」。「保存」用同一把 `zhupi.token` 往 review 仓 `home-data` 分支的 `edits.jsonl` 追加一行 `state:"pending"`（GET sha → 追加 → PUT，sha 冲突重试 2 次）。**token 需要 review 仓 Contents: Read and write**；没有时保存会报 403 并提示。
2. Mac mini：launchd `com.charlie.ai-home-edits` 每 5 分钟跑 `collectors/run_edits.sh` = `apply_edits.py`（按白名单改 vault 卡 frontmatter、按文本勾/取消里程碑、`updated` 改今天、`## 日志` 末尾追加 `- YYYY-MM-DD 主页编辑：…`；改了卡就立刻重采 dashboard）→ `build_home.py`。日志 `~/Library/Logs/ai-home/edits.log`。
3. 附言：有 `note` 的编辑字段照常落卡，然后标 `needs_hermes`。**apply_edits 不调 claude**；Hermes cron 取单子、派 Claude 处理、处理完标 done：

```bash
python3 ~/Developer/home/collectors/apply_edits.py --list-pending-notes          # JSON 数组：needs_hermes 且未 done
python3 ~/Developer/home/collectors/apply_edits.py --mark-done <id> --result "一句话结果"
```

幂等：账本按 `id` 合并，只处理 `state=pending`；卡已落但账本没推上时，下轮重放不会重复记日志。vault 读写超 30 秒整轮跳过，下轮再试。`edits.jsonl` schema 见 home-data 分支 README。

## 夜间养卡摘要

`python3 ~/Developer/home/collectors/daily_digest.py [--date YYYY-MM-DD] [--out PATH] [--max-chars 60000]`：把当天（America/Los_Angeles）Hermes 会话、Claude transcript、主页编辑摘成 `~/Library/Caches/ai-home/digest-<date>.md`，供 Claude 对照 Dashboard 卡更新；stdout 一行统计，无内容打 `digest <date> empty`。只读、纯标准库。

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
cp ~/Developer/home/launchd/com.charlie.ai-home.plist ~/Developer/home/launchd/com.charlie.ai-home-edits.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.charlie.ai-home.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.charlie.ai-home-edits.plist
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
