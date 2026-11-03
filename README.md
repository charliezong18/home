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
| `todos` | Google Tasks「今日待办」（My Tasks，只读）：`today` / `overdue`（due 升序）/ `nodate`（≤20 条），`counts` 为真实总数；只含未完成、不含 notes、title ≤80 字，「今天」按 America/Los_Angeles；`error` 非空 = 本轮同步失败、内容是上次成功的（gog 错误原文里的邮箱换成 `<email>`）；`failed` = 最近 24 h 经邸报勾完成 / 撤销、重试后仍失败且之后没再成功的任务 `[{task_id, error}]`（error ≤80 字、邮箱脱敏、不含标题），客户端据此提示「没同步成功，再勾一次」；从没采过则无此键，页面不显示该栏 |

`todos` 由 `collectors/collect_gtasks.py` 采集：调本机已授权的 `gog tasks list @default --json`（`@default` = My Tasks，可用 `AI_HOME_GTASKS_LIST` 换；分页，每页 100，30 s 超时），只读，不建/改/完成任务；快照只存 id/title/due。分段在 build 时现算，所以采集失败时分段仍按当天。只读演练：`python3 collectors/collect_gtasks.py --dry-run`（只打印三段计数）。

## 丢链接 CLI（给 Hermes 调）

```bash
python3 ~/Developer/home/collectors/add_link.py 'https://example.com/post' '周末读'
# → 打印一行：OK <标题>  /  OK 已在读物里：<标题>  /  FAIL <原因>；退出码 0 = 成功
```

标题抓取 10 秒超时，失败用 URL 当标题（仍算成功）。同一 URL 不重复记。页面只读 `home.json`，所以链接要等下一轮 build 收录后才在页面出现——最长 30 分钟（急的话手动 `launchctl kickstart`）。

## 日历 / Gmail 到期条目怎么来的（WP-08b）

Hermes cron 每天 07:00 / 18:00（America/Los_Angeles）用 `hd.py create --template life` 派一个 Sonnet 会话，读 Google Calendar 未来 14 天与 Gmail 最近 3 天，写进 `life.jsonl`；`build_home.py` 把归并后 `status=open` 的条目并入 `due[]`（source 为 `calendar` / `gmail`）。写入口唯一是 `collectors/life_ingest.py`：

```
python3 collectors/life_ingest.py add --json '[{"event_id":"<日历事件 id>","title":"示例会议","date":"2026-11-05","note":"14:00","source":"calendar"}]'
python3 collectors/life_ingest.py add --json '{"title":"示例会议","date":"2026-11-05","source":"calendar"}' --event-id '<日历事件 id>'
python3 collectors/life_ingest.py done --id d:cal-1a2b3c4d
python3 collectors/life_ingest.py list --window 14        # 只读
# add/done 加 --no-git 只写文件不碰 git（测试用）
```

**调用方应传日历事件的稳定 id**（Google Calendar 事件的 `id`，重复事件用实例 id；或 iCal UID）：字段 `event_id`（数组里每个对象各写一个），单个对象也可用 `--event-id`。有它时 id = `d:cal-` + sha1(`source|event|event_id`) 前 12 位，事件在日历里改了标题 / 时间也只更新同一条；`event_id` 只用来算 id，不写进账本。不传则退回按 `source+title+date` 哈希——标题改一次就多出一条新 id、旧条目不会关，到期栏会重复。`build_home.due_items` 另有兜底：`source=calendar` 的条目按 (日期, note, 规整后的标题) 归并，规整 = 全角转半角、去括注、去 emoji / 标点 / 空白，保留最后写入的那条。

条目可带 `"private": true`：事件推送（今天截止）与早晚报只显示「1 件私密事项」。不存金额、邮件正文、发件人邮箱：`title`/`note` 里出现金额或 `@` 整批拒绝。Gmail 第一周只在 cron 报告里给「建议」，不入账（模板内开关，Charlie 点头后改模板）。

## 服务 dibao（#187 A 期·核心）

Mac mini 常驻的标准库 HTTP 服务，`127.0.0.1:8766`，**不暴露、不鉴权、不托管页面**（#192 Q10=B：客户端只连 GitHub）。本地 home-data 工作树是真源，dibao 是本机唯一写者（上线前必须先停 `ai-home-edits`，否则双写）。
防跨站：带 `Origin` 头（或 `Sec-Fetch-Site` ≠ `none`）的请求一律 403，POST / PATCH 必须 `Content-Type: application/json`（否则 415）——浏览器里的网页因此碰不到它；`cli.py` / curl 加 `-H 'Content-Type: application/json'` 照常用。

```
客户端（网页 / iOS）──(PAT, contents API)──► GitHub review:home-data ◄──┐ push（≤60 s 防抖）
Mac mini dibao :8766
  ├─ 每 60 s git pull --rebase → 发现 pending 编辑 → 子进程落卡（30 s 硬超时）→ 重算 home.json
  ├─ 本机 HTTP（Hermes / Claude）→ 写本地账本 → 立刻落卡、重算 ────────────┘
```

「保存即生效」：本机调用 POST 即生效；客户端经 GitHub 的写入最长 60 秒落卡。账本 jsonl 在 `.git/info/attributes` 里设 `merge=union`，两边同时追加末尾不冲突。复用 `collectors/` 的 `data_lock / sync_down / apply_edits.validate / build_home.main / add_link`，不复制逻辑；读写 vault 只在 `apply_edits --worker` 子进程里。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/health` | `{ok, uptime_s, last_sync_down, last_sync_ok, last_sync_error, last_push, last_push_ok, last_push_error, worktree_dirty, rebasing, pending_edits, vault_ok, version, last_collect, collect_failures, push_failures, apns:{configured, devices, last_push, last_error}}`；不取数据锁。`ok` = 最近一轮 sync 与 push 均未失败、工作树干净且不在 rebase 中（`*_ok` 为 `null` 表示还没跑过） |
| GET | `/v1/home.json` `/v1/status.json` | 本地文件，`ETag`（sha256 前 16 位），`If-None-Match` → 304 |
| POST | `/v1/edits` | body = Edit 首行（`from`: `hermes`/`claude`）→ 校验 → 追加 → 同步落卡 → `200 {edit, project, generated}`；超时 `202 {edit:{state:"pending"}}`（60 s 内由同步循环补上）；校验不过 400 |
| POST | `/v1/readings` | `{url, why}` → `201` Reading；URL 已在账本 → `200` 原记录 |
| PATCH | `/v1/readings/{id}` | `{status}`（new/reading/done/dropped），id 可用账本 id 或 `r:…` |
| GET | `/v1/notes/pending` | = `apply_edits --list-pending-notes` |
| POST | `/v1/notes/{id}/done` | `{result}` = `--mark-done` |
| POST | `/v1/todos/{task_id}/complete\|undo` | `{id?, title?, from?}`（`id` = 幂等键，uuid 或 ≤64 个字母数字 `_` `-`，否则 400）→ 追加 `todo-actions.jsonl` 首行 → 执行 gog（**不持服务锁**；超时 / 起不来 / 5xx 退避 2·4·8 s 重试 3 次再标 failed）→ `200` done / `502` failed；已有一批在执行时不等，回 `202 pending`，下一轮 sync 处理 |
| POST | `/v1/push/test` | `{title?, body?, url?}` → 立刻发一条推送（APNs，无设备则 ntfy）；`200 {result, apns}`，没送达 `502`。**会真发** |
| POST | `/v1/digest/{morning\|evening}` | 立刻发早 / 晚报（今天已发过 `dup`）；`{dry_run:true}` 只返回内容。**会真发** |
| POST | `/v1/collect` | `{sources?:["menxia","dashboard"]}` → `202 {job}`，异步跑一轮（采集器子进程 120 s 超时 → build）；源名不对 400 |
| GET | `/v1/collect/{job}` | `{id, state: running/done/error, sources, result}` |

```bash
make install     # 建 ~/.config/dibao、~/Library/Logs/dibao，拷 plist，launchctl bootstrap，打 /health（由 Hermes 执行）
make health / make uninstall / make test   # test 含 node --test test/*.test.js（网页逻辑，需 Node ≥ 18）
python3 service/cli.py health | edits --json '…' | readings-add --url URL --why 理由 | notes-pending | note-done --id ID --result 文本 | collect | digest morning|evening [--dry-run|--send]
python3 service/dibao.py --no-push --once-sync   # 演练：配 AI_HOME_DATA=<临时 git 仓> DIBAO_PORT=<端口>，绝不指向真实 review-home-data
```

配置 `~/.config/dibao/config.toml`（样例 `service/config.example.toml`），环境变量 `DIBAO_*` 优先。日志 `~/Library/Logs/dibao/api.log`，1 MB × 3 轮转。回滚：`make rollback`（bootout dibao，把 `~/Library/LaunchAgents/_retired-dibao-<日期>/` 里旧两个 plist 放回并 bootstrap）。

**调度（WP-09b）**：采集线程每 `collect_secs`（缺省 1800，`DIBAO_COLLECT_SECS` / `--collect-secs`）跑一轮 `collect_menxia` → `collect_dashboard` → 重算 home.json，采集器仍是子进程（120 s 超时），`status.py` 记成败与 ntfy 告警不变。本地提交后 `push_debounce_secs`（60）内 push；连续失败 3 次推 ntfy（每天 ≤1 条）。日志 `~/Library/Logs/dibao/sched.log`（轮转同 api.log）。`collectors/run_all.sh` 保留，手动跑也安全（同一把 `data_lock`）。

### 推送（WP-11：APNs，ntfy 兜底）

`service/apns.py`（发送）+ `service/events.py`（事件与去重）。只用标准库 + 子进程：JWT 用 `openssl dgst -sha256 -sign` 签（DER → raw r||s，缓存 50 分钟），发送用 `curl --http2`（系统 curl 不支持 HTTP/2 时在 `[apns] curl` 填 `/opt/homebrew/opt/curl/bin/curl`）。

- **密钥**：Charlie 在 Apple Developer → Keys 建 APNs Auth Key，.p8 放 `~/.config/dibao/apns/AuthKey_<KEYID>.p8`（`chmod 600`，进程只读）。
- **配置**：`~/.config/dibao/config.toml` 加 `[apns]` 段（字段见 `service/config.example.toml`：`key_id`、`team_id`、`bundle_id`，可选 `key_path`、`curl`）。删掉整段 = 回到 ntfy-only。
- **设备**：每轮 `sync_down` 后读 home-data 的 `devices.jsonl`，同 `token` 后者生效，取 `active:true`；`env:"sandbox"` 发 `api.sandbox.push.apple.com`，否则 `api.push.apple.com`。APNs 回 410 或 400 `BadDeviceToken` → 追加 `{"token":…,"active":false,"reason":…}` 并本地提交（随 push 防抖推回）。403 令牌问题刷新 JWT 重试一次。日志只记 token 末 6 位与响应 `reason`。
- **事件**（`apns-collapse-id` = 去重键；已推键记在 `~/.local/state/dibao/pushed.jsonl`，`fold-`/`note-` 永久，带日期的键保留 7 天）：

  | 事件 | 触发 | 键 | 文案 | 深链 |
  |---|---|---|---|---|
  | 新「你拍」折 | 每次重算后 `folders.oldest.你拍` 出现新 `n`（首次部署静默记基线，不把现有折当新） | `fold-<n>` | 待你拍：#n 标题（私密折：「1 件私密事项」，不出折号） | `menxia://?pr=<n>` |
  | 附言处理完 | `POST /v1/notes/{id}/done` | `note-<id>` | 附言已处理：卡标题（含金额则「1 件私密事项」） | `menxia://home?id=<target>` |
  | 采集连续失败 ≥3 | 重算后 `sources[*].consecutive_failures` | `fail-<源>-<日期>` | 邸报采集 <源> 连续失败 | `menxia://home` |
  | 到期当天 | 本地 07:30 起每天一次，扫 `projects[].deadline` 与 `due[].date` | `due-<id>-<日期>` | 今天截止：标题（含金额或 `private: true` 则「1 件私密事项」） | `menxia://home?id=<id>` |
  | 早报 | 洛杉矶 08:00，30 分钟内补发 | `digest-morning-<日期>` | 今天到期 / 3 天内到期 / 待你拍（折数 + 最老一折）/ 过期待处理 / 今日待办计数 | `menxia://home` |
  | 晚报 | 洛杉矶 21:30，30 分钟内补发 | `digest-evening-<日期>` | 今天进展（画可的折、经邸报勾完成的待办数）/ 今天新到的折 / 明天第一件事 | `menxia://home`（App 暂无跳「待审」tab 的深链） |

- **早晚报**（`service/digest.py`，纯函数；`Service.digest_tick` 跟到期扫描同一个每分钟线程）：时区固定 `America/Los_Angeles`（不看系统时区）。同一天每类最多一条：键带日期记在 `pushed.jsonl`，重启后读回不重发。Mac mini 睡眠 / 服务重启错过时间点：30 分钟内醒来补发，超过就当天跳过。送达失败不记账，窗口内每分钟重试。当天没内容只发一行（「今天没有到期和待拍」/「今天没有新进展，明天暂无安排」）。`home.json` 缺失、`generated` 超过 3 小时，或 `menxia` / `dashboard` 连续失败 ≥3 次 → 不下「空日」结论，改发一行「邸报数据过期，早报暂缺（原因）」并记日志（`cli.py digest --dry-run` 不做这项检查）。
  - **脱敏**（早晚报与上表三类事件推送共用 `digest.fold_private` / `item_private`）：折带 `签:身份` / `签:健康` / `签:钱`（代码内置），或本机 `~/.config/dibao/config.toml` 的 `[digest] protected_qian` / `protected_proj` 里设的额外受保护 label（不进仓库，缺省空；写法见 `service/config.example.toml`），或 `due[]` 条目带 `private: true`，或任何条目标题含金额（`$120`、`300 元`、`3万`、`USD 300`、`三千块`、`1,200`…）→ 正文只写「N 件私密事项」，不出标题和折号（锁屏与展开都一样）。
  - **数据**：`collect_menxia` 给折补 `qian`（`签:` 标签值），另取一页最近更新的已关 PR → `folders.merged_recent`（近 2 天合并的折，晚报「今天画可」用；取不到不影响主采集，晚报就不写这一项）。「今天新到」= 未关折里 `created` 落在洛杉矶今天的（取自 `folders.recent_open`：全部未关折按 `created` 降序前 50，含 `qian`；旧版 home.json 没有这个键时退回 `folders.oldest`）。
  - **演练 / 手动**：`python3 service/cli.py digest morning --dry-run`（本地读 home.json 与 `todo-actions.jsonl` 打印标题 / 正文 / 是否今天已发，不连服务、不发、不写文件）；`cli.py digest evening --send` 经 `POST /v1/digest/evening` 立刻发（**会真发**；今天已发过返回 `dup`）。`POST /v1/digest/{kind}` 带 `{"dry_run": true}` 只返回内容。

- **ntfy 兜底**：没有 active 设备（含未配置 `[apns]`）或所有设备都失败 → 同一条走 `collectors/notify.send` 发到 config.toml `[ntfy]` 段配的 topic（topic 是凭据，不进仓库；过渡期 `legacy_topic` 双发），`Click` 头带深链；APNs 至少一台成功就不发 ntfy。例外：采集失败事件**不**走 ntfy（`status.py` 在连败 3 次时已自己推过 ntfy，再发一遍是重复）。`AI_HOME_NTFY_DRYRUN=1` 时 ntfy 只打印。未送达的事件不记入去重集合，下次扫描重试。
- **手动发一条（会真发）**：`python3 service/cli.py push-test --title 测试 --body 门下推送 --url 'menxia://home'`（走 `POST /v1/push/test`，所以 `/health.apns.last_push` 会更新）。`/health` 的 `apns` = `{configured, devices, last_push, last_error}`。

### 服务化切换步骤（给 Hermes）

1. `make install`（建目录、拷 plist、bootstrap dibao、打 `/health`）。
2. `make retire`（先确认 dibao `/health` 通过，再 bootout `com.charlie.ai-home` / `ai-home-edits`，两个 plist 挪进 `~/Library/LaunchAgents/_retired-dibao-<日期>/`）。`com.charlie.menxia-approvals` 不动。若第 1 步已装过，直接手动 `make health` 后只做 retire 的 bootout/mv。
3. Hermes cron `ai-home-pending-notes.sh` 脚本体改成 `python3 ~/Developer/home/service/cli.py notes-pending`（cron 本身由 Hermes 改）。处理完用 `cli.py note-done --id ID --result 文本`。
4. 养卡 cron 末尾的 `run_all.sh` 改成 `python3 ~/Developer/home/service/cli.py collect`。
5. 验收：`launchctl list | grep com.charlie` 无 `ai-home` / `ai-home-edits`、有 `com.charlie.dibao`；`make status`；`cli.py collect` 后 `/health` 的 `last_collect` 更新；网页改一张卡 `next`，≤90 秒内 vault 卡改、`home.json` 重算并 push。回滚 `make rollback`。

## ID 约定

页面上每条东西点开（或行首/行末的小 ID）都能复制一个短 ID，粘给 Hermes 就知道指哪一条。统一前缀、ASCII 前缀便于语音/复制：

| ID | 指什么 | 来源 |
|---|---|---|
| `#<PR号>` | 折（含弘文馆） | 门下 PR 号，如 `#179` |
| `p:<slug>` | Dashboard 项目卡 | 卡 frontmatter `id:` 优先；否则文件名去 `proj:` 前缀/emoji/标点、小写、空白→`-`（中文保留）；冲突加 `-2`、`-3`。写进 `home.json` 每张卡的 `id` |
| `r:<id>` | 丢的链接 | 账本 id 去掉 `link-` 前缀，如 `r:20261013-b559`；`home.json` 里是 `rid` 字段 |
| `d:<来源>-<slug或hash8>` | 到期项（`home.json` 的 `due[]`） | `d:countdown-<slug>` 倒数日、`d:backlog-<slug>` Backlog「⏰ 有窗口的」；`d:cal-<hash8>` / `d:bill-<hash8>` 留给 WP-08b。schema 见 review-home-data README |

Hermes 解析（读本地 `~/Developer/review-home-data/home.json`，不走网络；找不到退出码 1）：

```bash
python3 ~/Developer/home/collectors/resolve.py '#179' 'p:门下-menxia' 'r:20261013-b559'
# 每个 ID 一行 JSON：{id, kind, title, url, meta} 或 {id, error:"not found"}
# url：折=门下深链；项目卡=vault 绝对路径；读物=原链接。折只覆盖 home.json 收录的（每类最旧 8 折 + 弘文馆）
```

## 深链、快捷键、过期待处理

- **深链** `https://charliezong18.github.io/home/?id=<ID>`：`p:<slug>` 直接开项目弹窗（找不到时页脚提示一行）；`%23<PR号>`（即 `#179`，`#` 在 URL 里须写成 `%23`）跳门下对应折；`r:<id>` 跳原链接。开弹窗时地址栏自动写 `?id=`，关弹窗清掉。
- **快捷键**：`1`–`4` 切 tab（头条/折务/项目/读物），`r` 重新读取；输入框聚焦或弹窗打开时不触发。上次所在 tab 记在 `localStorage` 的 `dibao.tab`，带 `?id=` 打开时落在头条。
- **过期待处理**：到期 tab 里的分区，收 `deadline` 早于今天**超过 14 天**的项目卡与折（`OVERDUE_AFTER`），不自动清、日期原样保留，过期最久的在前；头条与「14 天内到期」栏不含它们。
- **本地预览** `?demo=1`：不读网络、不要 token，用 `app.js` 里的虚构夹具（含 3 张过期项目卡、1 折带 deadline）。夹具内联在 JS 里，因为页面 CSP 的 `connect-src` 只放行 `api.github.com`，同源 fetch JSON 会被拦。

## 编辑流（弹窗编辑 → 保存 → 触发更新）

1. 页面：项目卡弹窗「编辑」可改 `next` / `status` / `tier` / `deadline`、勾里程碑，另有「给 Hermes 的话」。「保存」用同一把 `zhupi.token` 往 review 仓 `home-data` 分支的 `edits.jsonl` 追加一行 `state:"pending"`（GET sha → 追加 → PUT，sha 冲突重试 2 次）。**token 需要 review 仓 Contents: Read and write**；没有时保存会报 403 并提示。
2. Mac mini：launchd `com.charlie.ai-home-edits` 每 5 分钟跑 `collectors/run_edits.sh` = `apply_edits.py`（按白名单改 vault 卡 frontmatter、按文本勾/取消里程碑、`updated` 改今天、`## 日志` 末尾追加 `- YYYY-MM-DD 主页编辑：…`；改了卡就立刻重采 dashboard）→ `build_home.py`。日志 `~/Library/Logs/ai-home/edits.log`。
3. 附言：有 `note` 的编辑字段照常落卡，然后标 `needs_hermes`。**apply_edits 不调 claude**；Hermes cron 取单子、派 Claude 处理、处理完标 done：

```bash
python3 ~/Developer/home/collectors/apply_edits.py --list-pending-notes          # JSON 数组：needs_hermes 且未 done
python3 ~/Developer/home/collectors/apply_edits.py --mark-done <id> --result "一句话结果"
```

幂等：账本按 `id` 合并，只处理 `state=pending`；卡已落但账本没推上时，下轮重放不会重复记日志。vault 读写超 30 秒整轮跳过，下轮再试。`edits.jsonl` schema 见 home-data 分支 README。

### 读物状态行（读完 / 不读了）

「丢的链接」每行末尾有两个小按钮。点击后乐观隐藏该行，并用同一套 GET sha → 追加 → PUT 往 `home-data` 的 `readings.jsonl` 追加 `{"id":"link-…","status":"done"|"dropped","ts":"…Z","from":"web"}`；失败则恢复该行并在行内显示错误。账本 append-only、同 id 后者生效，`build_home.load_readings()` 已按此归并并滤掉 done / dropped（状态行只有 id/status/ts/from 也能合并）。iOS 邸报写同形记录（`from:"ios"`）。`?demo=1` 下只做本地隐藏，不发请求。

误点复活：再追加一行同 id 的 `{"status":"new"}`（`add_link.py` 同 id 追加亦可），不删行。

## 夜间养卡摘要

`python3 ~/Developer/home/collectors/daily_digest.py [--date YYYY-MM-DD] [--out PATH] [--max-chars 60000]`：把当天（America/Los_Angeles）Hermes 会话、Claude transcript、主页编辑摘成 `~/Library/Caches/ai-home/digest-<date>.md`，供 Claude 对照 Dashboard 卡更新；stdout 一行统计，无内容打 `digest <date> empty`。只读、纯标准库。

## 门下画可接管（approvals.py）

画可（merge）不等于完成，也不等于概括授权。`collectors/approvals.py` 由 launchd `com.charlie.menxia-approvals` 每 5 分钟跑 `scan`：gh api 增量拉 closed PR（游标 + 6 小时回看，失败不前移游标，停机重启后自动补漏），每个 merge 按 `repo#PR@merge_sha` 幂等记一张单。`kind:读物/参考` 只进 `references`，不生成执行单。

单子带：PR body 的「待你拍板」「目的地」、他在折里的判（原文，去掉续读标记和 `**回话**`）、归属 Hermes 会话（merge 前 Hermes 消息里提到该折分支名或 `review/pull/N` 最多的非 cron 会话；并列或无命中时为 null，由 Hermes `set-owner`）。

状态：`pending 待接手 → executing 执行中 → verifying 验收 → done 完成`，`blocked 阻塞` 旁路。进 executing 必须 `--scope`（Hermes 读全文后写明范围）；done 只能从 verifying 来且必须 `--evidence`。`--event-id` 幂等。`--writeback` / `sync` 只改门下 label（pending→`wait:agent`；执行中/验收另加 `agent:claimed`；阻塞→`agent:blocked`+`wait:你拍`；完成→去掉 wait/agent），不发会话区评论。launchd 那份 scan 不带 `--writeback`。

台账 `~/.local/state/menxia-approvals/approvals.json`、`events.log`；日志 `~/Library/Logs/ai-home/approvals.log`。命令见 `approvals.py` 文首；单测 `python3 collectors/test_approvals.py -v`。

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
