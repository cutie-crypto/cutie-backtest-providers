# ARM-C0-P：provider 多空比 / 清算模板在 1h 下读小时流（tokenbeep-impl → tokenbeep-codex-3；provider 仓，≤55 轮，到 45 轮提交并回报）

常备令 `/Users/tf/Documents/bitbucket/TokenBeep/.claude/rules/herdr-line-standing-orders.md`；provider 仓 `/Users/tf/Documents/bitbucket/cutie-backtest-providers`，`main` 只走 PR。`git fetch`，从 **origin/main（@0e8b0ce 或更新）** 用 `git worktree add` 新开树 `../cutie-backtest-providers-arm-c0-p`，分支 `impl/arm-c0-p`。大文件（cutie_backtesting_provider.py 上万行）先 `grep -n` 定位，按段读，禁止通读。
探路底稿：`/tmp/arm-c0-ds/REPORT.md`（批②）；统领 1013 批注：进 PIN-18（与 PSF、WARMUP-B 同窗），**PR 须在 10-15 JST 12:00 前转 ready，且 6 条用例全绿**；赶不上就留 PIN-19。provider 在钱路径上游，**合 PR 前统领亲读三段 diff**。

## 现状（按 pin 30d6a07 / main 0e8b0ce）
- `backtesting-py/strategy_top_long_short_reversal.py:29`、`backtesting-py/strategy_liquidation_reversal.py:39`：`CENTRAL_INTERVAL` 固定 1d。
- `backtesting-py/cutie_backtesting_provider.py:1732` 统一取数（available_at = ts + 步长）；`:11869` top_long_short 绑定、`:11934` liquidation 绑定。
- 平台已前移：`cutie-server/services/top_long_short_precheck.py:125/147`、`liquidation_reversal_precheck.py:148/179/189` 在 1h 下查小时数据（清算默认 30 天 = 720 根，fixed 模式还要再往前多取 1 根），4h / 1d 仍查日线。平台读数入口：`cutie-server/handlers/internal/market_data.py:189-209` 的 `GET /v1/internal/market-data/metrics`，参数是 symbol / exchange / metric / interval / 闭区间，默认 1000 条，上限 5000。provider 分块取数时边界根不能丢。
## 口径（统领裁定，不得偏离）
1. 主周期 1h：两模板读**小时流**（同族同 interval='1h'）；top 仍用账户比（ACCOUNT ratio），清算仍用 long USD，回看 `lookback_days × 24` 根。
2. H 时刻的数据在 **H+1** 才可用（available_at = ts + 1h），不得提前。
3. 指标缺根、值为空、时间戳重复，一律拒跑，返回明确的 reason，**不补 0、不悄悄回退到 1d**。
4. 4h / 1d 主周期保持现有日线语义不变（逐字节对比现有金样）。
5. 只改这两个 strategy 及其 bind / schema / assumptions 和用例；其它模板与金样零变化。
## 用例（6 条，全部要绿；每条写明变异点，变异后必须红）
① hourly_metric_available_at_H_plus_1（变异：available_at=ts）；② hourly_bind_uses_exact_family_interval（变异：改回 1d，或把族换成 global / short）；③ hourly_metric_gap_rejects（缺一根 ⇒ 拒跑；变异：补 0 后继续）；④ hourly_metric_duplicate_or_null_rejects（变异：去重后继续）；⑤ chunked_fetch_keeps_boundary_bar（跨 5000 上限分块时首尾根都在；变异：边界用开区间）；⑥ daily_and_4h_unchanged（4h / 1d 金样字节不变）。
## 交付
- 门：定向用例 → 两 strategy 相关测试整文件 → 收尾跑一次全量 `cd <wt>/backtesting-py && python3 -m pytest tests -q -p no:cacheprovider > /tmp/arm-c0-p/full.log 2>&1; echo EXIT=$?`。全量与 main 基线对比，新增失败为 0（基线本来就有的失败要附 nodeid）。
- 异模型 review：Claude Sonnet 5.5 一轮只读，落档 provider `docs/reviews/ARM-C0-P-review.md`。
- 开 PR，base=main，先 draft；6 条绿、全量没有新增失败后转 ready。**不合并**。
- **另交三段 diff 文件给统领亲读**：`/tmp/arm-c0-p/diff-gate.patch`（拒跑 / 可用时刻闸门）、`/tmp/arm-c0-p/diff-bind-schema.patch`（bind / schema / assumptions）、`/tmp/arm-c0-p/diff-tests.patch`（用例）。
- 回报 `/tmp/arm-c0-p/REPORT.md`：首行 `ARM-C0-P-DONE <7 位 sha>`，八项 ≤25 行，末行 ACK；第 1 项写 PR 号；pane 里只回首行。
