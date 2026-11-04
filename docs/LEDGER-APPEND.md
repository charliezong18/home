# 账本追加契约（客户端经 GitHub Contents API 追加 jsonl）

适用：网页 `app.js` 的 `appendLine` / `readLedger`，以及 iOS `DibaoStore.swift` 的同等实现（应照本文镜像）。
账本：home-data 分支上的 `edits.jsonl`、`readings.jsonl`、`todo-actions.jsonl`。只追加，同 id 后者生效。

## 为什么要有这份契约

Contents API `GET /repos/{owner}/{repo}/contents/{path}` 对 **> 1 MB** 的文件返回 `content: ""`、
`encoding: "none"`（`sha` 和 `size` 仍然正确）。如果把空 `content` 当成空文件、再带着这个有效的 `sha`
去 PUT，GitHub 会接受，结果是**整个账本被覆盖成新增的那一行**，历史静默丢失（ultrareview H1）。

核心规则：**读到的字节数必须等于 API 报的 `size`，否则不写。** 绝不把「读不全」当成「空文件」。

## 读（得到完整文本 + sha）

1. `GET contents/{path}?ref=home-data`，`Accept: application/vnd.github+json`，不走缓存。
   - `404` → 文件不存在：`text = ""`，**不带 sha**（PUT 会新建）。只有 404 才能这样处理。
   - 其他非 2xx → 报错，不写。
2. 校验响应：`type == "file"`、`sha` 非空、`size` 是非负整数；否则报错，不写。
3. `size > LEDGER_MAX`（25 MiB）→ 报错「需先轮转」，不写。
4. 取内容：
   - `encoding == "base64"` 且（`content` 非空 或 `size == 0`）→ 解码 `content`（先去掉换行等空白）。
   - 否则（`encoding` 为 `"none"` / 缺失，或 `content` 为空而 `size > 0`）→
     `GET /repos/{owner}/{repo}/git/blobs/{sha}`（`Accept: application/vnd.github+json`，≤100 MB）。
     要求 blob 响应 `encoding == "base64"` 且 `sha` 与第 1 步相同；否则报错，不写。
5. **解码后的字节数必须 == `size`**，否则报错「读不完整」，不写。
6. 以严格 UTF-8 解码（非法字节报错，不写）。

> 不用 raw 媒体类型（`application/vnd.github.raw`）：它不带 `sha`，还得再请求一次，且两次之间文件可能已变。
> 用同一个 `sha` 取 blob，读到的内容与将要 PUT 的 `sha` 一定对应。

## 写

1. `text` 非空且不以 `\n` 结尾 → 先补一个 `\n`。
2. 新内容 = `text + 一行 JSON + "\n"`，base64 编码。
3. `PUT contents/{path}`，body `{message, branch: "home-data", content, sha?}`（`sha` 取自读这一步；404 时省略）。
4. `409` / `422`（sha 已过期，别人同时写了）→ 从「读」重新来，共最多 3 次；仍失败则报错给用户。
5. `401` / `403` / `404` 等 → 直接报错，不重试。

## 错误呈现

任何「拒绝写入」都必须让用户看到（网页显示在保存按钮旁的错误区），不能显示「已保存」。
本地乐观更新只在 PUT 成功后才做。

## 测试用例（iOS 应有同等用例）

见 `test/ledger_append.test.js`：
- 小文件 base64 → 原样保留 + 追加；不请求 blob。
- `encoding: "none"`、`content: ""`、`size > 1 MB` → 读 blob，PUT 内容 = 旧全文 + 新行，`sha` 不变。
- `content` 空、`size > 0`、blob 读不到 → 拒绝，不 PUT。
- 解码字节数 ≠ `size` → 拒绝，不 PUT。
- `size > LEDGER_MAX` → 拒绝，不 PUT。
- 404 → 新建，PUT 不带 `sha`。
- `size == 0` 的空文件 → 正常追加，带 `sha`。

## 后续（未在本次修复内）

- **轮转**：三个账本都只追加不轮转，迟早碰到 `LEDGER_MAX`。轮转需要 dibao / build_home 读多个分片
  （如 `edits.jsonl` + `edits-YYYY.jsonl`），会改读取侧，单独做。在此之前，`LEDGER_MAX` 让客户端大声失败而不是静默丢数据。
- 长期更稳的做法是客户端不再整份 PUT，改由 dibao 合并（例如每个客户端写各自的小文件），同样单独设计。
