# launchd/_retired

被 dibao 服务取代的旧定时器（#192 Q11）。**不要 bootstrap。** 回滚走 `make rollback`。

- `com.charlie.ai-home.plist`：每 1800 s 跑 `collectors/run_all.sh`（现为 dibao 的采集线程）
- `com.charlie.ai-home-edits.plist`：每 300 s 跑 `run_edits.sh`（现为 dibao 的 60 s 拉回 + 落卡）
- `com.charlie.menxia-approvals` 不在此列，仍保留。
