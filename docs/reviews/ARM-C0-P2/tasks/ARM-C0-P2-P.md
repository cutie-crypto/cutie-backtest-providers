# ARM-C0-P2-P：1h 清算证据压缩为「完整事件 + 24h 段摘要 + 哈希」，超限给 reason（tokenbeep-impl → tokenbeep-codex-6；provider；统领 1013 批注照方案 B）

常备令 `/Users/tf/Documents/bitbucket/TokenBeep/.claude/rules/herdr-line-standing-orders.md`。≤45 轮，到 36 轮没做完就提交推送已完成部分并回报剩余。
## 基线
provider 仓 worktree `/Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p`，分支 `impl/arm-c0-p`（PR #102 draft，@d70fb82）。先 `git fetch`；若 origin/main 已前进，rebase 到 origin/main（不 force push 以外的办法解决不了时停下回报）。不碰 main，不合 PR。每条命令先 cd 到 `backtesting-py`。
## 底稿
`/tmp/arm-c0-p2/REPORT.md`（探路全文，证据在 /tmp/arm-c0-p2/：sizes.txt、proposal-*.json、EVIDENCE.md）。
## 改法（方案 B，统领已准）
- 清算 1h 证据：触发尝试 / 实际成交 / 拒单 / 原生退出 / 持仓阻断 / 提前结束这类事件**全部保留**；常规未触发小时不再逐条写，改成每 24h 一段摘要：首末小时、count、状态与原因计数、量 / 阈值 / 跌幅的 min / max、该段哈希；另加全轨迹哈希和外部指标哈希（含 720h 预热）。
- 压缩只在 `cutie_backtesting_provider.py:11733` 之后做（成交归属和缺状态守卫之后），不提前裁。
- raw_report 子对象加 `evidence_version` / `mode`，新旧兼容；trades / metrics / equity_curve / manifest 逐字节不变。
- 1MB 上限保持 fail-closed，不静默裁事件；超限时 `limitations.reason=report_evidence_too_large`，带 field / limit_bytes；原「shorten the backtest range」报文保留。
- 不碰 funding / OI / ETF / netflow 同形（统领裁登 BACKLOG 不动）。
## 用例（按底稿 5 条）
hourly_sparse_3606_4326_budget、summary_conservation_hash、retain_attempts_native_and_fill、consumers_and_legacy、limit_message_and_dense_fail_closed（provider 侧覆盖 reason 字段；平台 / 两端文案由 ARM-C0-P2-S 单做）。每条指名变异红，cp 还原后回绿。
## 门与回报
快信号单文件 `python3 -m pytest tests/<文件> -q -p no:cacheprovider > /tmp/arm-c0-p2p/t.log 2>&1; echo "EXIT=$?"`；收尾在仓根口径全量一次 `python3 -m pytest tests -q -p no:cacheprovider > /tmp/arm-c0-p2p/full.log 2>&1; echo "EXIT=$?"`，判据：相对 main 基线新增失败 0。金样有变化的逐个列出（应只限清算 1h 的 raw_report 形状），不自行重算别的模板金样。异模型 review：Claude Sonnet 5.5 一轮，落 PR 描述或 `docs/` 下 review 文件。
提交推 `impl/arm-c0-p`（更新 PR #102，仍 draft）。**合 PR 前统领要亲读三段 diff**：①证据压缩、②reason、③用例，回报里给三段的文件:行范围。
回报 `/tmp/arm-c0-p2p/REPORT.md`：首行 `ARM-C0-P2-P-DONE <7 位 sha>`，八项 ≤25 行，另加一行 3606h / 4326h raw_report 实测字节数；pane 只回首行。

## impl 解阻修订（2026-10-13，用户本会话授权）
任务书 rebase 指令写错：checkout -B impl/arm-c0-p origin/impl/arm-c0-p，丢弃本地 rebase，merge --no-ff origin/main，普通 push，不 force。异模型 review 改为 opencode 或 agy 一轮（Codex 不自审，也不用 Claude）；落 review 文件，覆盖 /tmp/arm-c0-p2p/REPORT.md。
