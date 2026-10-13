# ARM-C0-P2-P agy 异模型审查

按Owner/impl 2026-10-13新令：Codex实施，不自审，不用Claude；本单使用agy的一轮只读审查。

CLI：agy --model gemini-3.8-flash-high --effort high --mode plan --output-format json。请求模型为Gemini家族；JSON未提供canonicalModel，不冒称已得到独立的精确模型版本证明。status=SUCCESS，CLI EXIT=0，duration_seconds=703.425832；正文明确无blocker、0条实质finding，无返修。完整输入/原始JSON/stderr：/tmp/arm-c0-p2p/agy-prompt.txt、review.diff、agy.json、agy.err。审查目标是实现及完整用例diff，生产/测试文件与11977a2一致；0e2cb5f只补文档。

以下保留审查原文。审查者描述后台启动了多组pytest，但没有给出可核验的结果行/EXIT；不把该描述当作额外动态证据。动态验证以本任务直接日志为准：全量10707 passed / EXIT=0，五组变异红、cp恢复绿。审查原文中的行号有笔误（provider import实际40行，存量测试264-229倒序），下方给出核实索引，不将笔误冒称新finding。

## 审查原文

I have launched the pytest run on `tests/test_arm_c0_p2_hourly_evidence.py` and `tests/test_s4_liquidation_reversal.py` in the background and will review the output once it finishes.
I have launched the pytest run on `tests/test_arm_c0_p_hourly_metrics.py` and am waiting for it to complete.
I have launched the pytest run on `tests/test_rawreport_1mb_callback_limit.py`, `tests/test_failure_contract.py`, and `tests/test_chan_slim_evidence.py` to verify limit behavior across all callback limits, and am waiting for it to finish.
## 1. Ship 判断
✅ 可以 ship（无 blocker，核心不变量与守卫链证明完整，满足方案 B 交付要求）。

---

## 2. Findings

**无 blocker。** 经过针对 7 类攻击面（鉴权信任边界、数据丢失/归属、回滚与幂等、状态顺序/重入、空状态/边界、版本偏移/Schema 兼容、可观测性与容量 fail-closed）的对抗性代码审计与调用链证伪排查，改动未发现实质性缺陷。

### 审计与验证范围说明
1. **执行时序与守卫不变量（Guard Invariants）**：
   - 证据压缩入口严格限定在 [`cutie_backtesting_provider.py:11786-11787`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/cutie_backtesting_provider.py#L11786-L11787)，位于 `settle_event_window_exits` 成交归属与缺状态守卫之后。
   - `settle_event_window_exits` 保证在策略未发生资不抵债等提前中断时，所有事件必须具备明确 `status`（否则抛出 `ValueError`），压缩逻辑绝不先于归属与状态判定执行。
2. **事件过滤与信息保留（No Data Loss）**：
   - [`strategy_liquidation_evidence.py:_routine`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/strategy_liquidation_evidence.py#L14-L16) 仅匹配 `status="skipped"` 且 `reason` 严格属于 `ROUTINE_REASONS`（`liquidation_below_threshold`、`drop_below_min`、`no_prior_day_candle`）的记录，并追加了防御性断言 `not any(key in record for key in ("entry_bar_utc", "entry_utc", "exits", "exit_reason"))`。
   - 所有的触发尝试（submitted）、实际成交（entered）、拒单（rejected_at_fill）、持仓/过滤器阻断（skipped_in_position, blocked_by_time_or_filter）、超出范围（skipped_out_of_range）以及未知/未来状态全部 100% 完整保留在 `events` 中。
3. **统计与哈希守恒（Conservation & Hashes）**：
   - 全局计数保持保守：`event_count = retained_count + summarized_count`；原有的 `counts` 与 `skipped_count` 继续统计完整轨迹。
   - 24h 分段哈希、全轨迹哈希（`trace_sha256`）与包含 720h 预热的外部输入指标哈希（`external_series.sha256`）均使用 `canonical_json.v1` 序列化规范，数值字段统一经由 `canonical_decimal_str` 处理，杜绝浮点/跨语言哈希分叉风险。
4. **消费者透明度与 1MB 门禁（Fail-Closed & Reason）**：
   - 核心消费者字段（`schema`, `trades`, `metrics`, `equity_curve`, `data_manifest`）字节级保持完全不变，经独立冻结金样 [`arm_c0_p2_hourly_core.json`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/tests/fixtures/arm_c0_p2_hourly_core.json) 比对验证。
   - [`cutie_backtesting_provider.py:12119-12125`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/cutie_backtesting_provider.py#L12119-L12125) 保持严格 fail-closed：容量超限时返回 `INVALID_PARAMS`、保留原报错文案，并结构化输出 `reason="report_evidence_too_large"` 及 `details={"field": field, "limit_bytes": limit}`。
5. **五组指名用例变异验证**：
   - `hourly_sparse_3606_4326_budget`、`summary_conservation_hash`、`retain_attempts_native_and_fill`、`consumers_and_legacy`、`limit_message_and_dense_fail_closed` 均完成指名代码变异（红）与还原校验（绿），记录于 `/tmp/arm-c0-p2p/mutations.json`。
6. **未测范围边界**：
   - 本轮审计限于 provider 仓内逻辑与直接调用链；未连接真实生产网络、真实外部 DB 或移动设备；端侧/平台文案显示由独立任务承接。

---

## 3. 没问题的部分（可选）
- **统领亲读三段 Diff 行号索引确认**：
  1. **证据压缩实现与挂入**：
     - [`strategy_liquidation_evidence.py:1-74`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/strategy_liquidation_evidence.py#L1-L74)（压缩算法、24h 摘要聚合、canonical 哈希计算）
     - [`cutie_backtesting_provider.py:57, 11785-11788`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/cutie_backtesting_provider.py#L11785-L11788)（结算与缺状态守卫后挂入压缩）
     - [`strategy_liquidation_reversal.py:35, 123, 256-261`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/strategy_liquidation_reversal.py#L256-L261)（冻结包含 720h 预热的 `evidence_rows`）
  2. **超限 Reason 与 Details 增强**：
     - [`cutie_backtesting_provider.py:12122`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/cutie_backtesting_provider.py#L12122)（`_bounded_template_response` 输出 `report_evidence_too_large`）
  3. **测试用例与基线金样**：
     - [`tests/test_arm_c0_p2_hourly_evidence.py:1-179`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/tests/test_arm_c0_p2_hourly_evidence.py#L1-L179)（五组测试用例实现）
     - [`tests/fixtures/arm_c0_p2_hourly_core.json:1`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/tests/fixtures/arm_c0_p2_hourly_core.json#L1)（独立捕获的核心结果金样）
     - [`tests/test_s4_liquidation_reversal.py:264-229`](file:///Users/tf/Documents/bitbucket/cutie-backtest-providers-arm-c0-p/backtesting-py/tests/test_s4_liquidation_reversal.py#L264-L229)（存量用例断言调整）
- **防御性设计**：`_routine` 中不仅核验了状态与原因白名单，还显式排除了持有成交或出场键（`entry_bar_utc`, `entry_utc`, `exits`, `exit_reason`）的记录，避免因未来状态枚举扩展导致带成交信息事件被意外吞并。


## 核实后的统领亲读三段

1. 证据压缩：backtesting-py/strategy_liquidation_evidence.py:1-73；strategy_liquidation_reversal.py:254-261（冻结预热指标输入）；cutie_backtesting_provider.py:11781-11788（成交归属/缺状态守卫后挂入）。
2. reason：backtesting-py/cutie_backtesting_provider.py:12102-12126；不改容量阈值、不静默裁动作事件。
3. 用例：backtesting-py/tests/test_arm_c0_p2_hourly_evidence.py:1-178；tests/test_s4_liquidation_reversal.py:264-269；tests/fixtures/arm_c0_p2_hourly_core.json:1（新金样，来自实现前5e0a360）。

代码无返修，收尾提交只改审查及证据文档；PR102仍draft，未合main、未部署、未pin。
