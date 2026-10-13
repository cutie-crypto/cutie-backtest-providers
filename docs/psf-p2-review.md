# PSF-P2 候选审查：阻断，禁止合入或 pin

基线 provider `origin/main 30d6a07`；候选分支 `impl/psf-p2`。候选没有达到 PSF-P2 完成标准，尚未合并、部署、修改 pin。

## 停止点

按 PSF-P1 金样纪律「若变动面超出出场价 / 出场时间，停下回报」，在海龟双向金样发现入场变化后停止实现和重算。`tests/fixtures/turtle_both_golden.json` 笔数 6→6，但原第 51 根开盘 91 的空单变成第 24 根开盘 100；更早的止损释放持仓，使第 24 根的反向信号可交易。没有自行延长禁入窗口或接受新入场。需要统领裁定该因果行为是否允许，或将海龟拆出本批。

海龟 long/short 均 6→6，按 EntryBar 排序的入场根和入场价不变，各 4 笔出场根或出场价变化。both 也有 4 笔出场变化，但因入场变化阻断。**所有金样文件保持原样，改动文件数 0；没有手改 JSON，也没有执行重算写入。** 现有 snapshot 函数的重演仅写临时审计文件，历史金样未被覆盖。

130 个旧 capture 场景重演：event_window 的两个场景在基线与候选都缺少 events 输入，其余 128 场景笔数与入场均无变化；此集合不包含海龟，不能作为海龟通过证据。

## 本地验证

- 编译与 `git diff --check`：EXIT 0。
- `test_psf_p2_template_fills.py` 18 条；`test_psf_p2_remaining_fills.py` 18 条：合成 OHLC，无网络，共 **36 passed / EXIT 0**。形态多空和斐波那契另覆盖单止盈跳空、入场根触发和交易出场标签；其余每族止损、跳空、双触。F5 默认没有单一止盈保护价，both 场景仅验证大振幅止损，不证明与 VWAP 信号退出竞争；仍需补对应边界。
- 八处变异：pattern、fibonacci、bottom、divergence、chan、F5、F6 各撤 native 开关；turtle 撤 sl 挂单。对应 `test_template_protective_fill[stop-pattern|stop-fibonacci]`、`test_remaining_template_protective_fill[stop-bottom|stop-divergence|stop-chan|stop-f5|stop-f6|stop-turtle]` 均 **RED_EXIT=1**，`cp` 还原后各 **1 passed / EXIT 0**。
- 旧扩展用例：**23 failed / 238 passed / EXIT 1**，包含旧成交/优先级断言和旧指纹，不改判为绿。
- provider 全量收尾仅一次：**518 failed, 10158 passed, 164 warnings in 311.68s (0:05:11)；EXIT 1**。尚未逐条完成分类、返修或重算，不可合入。
- TokenBeep 文档树规则门：**GATE_RESULT=GREEN target=all checks=25 red_count=0 / EXIT 0**。此门不证明 provider 候选可上线。
- 日志与审计：`/tmp/psf-p2/` 的 compile-all.log、all-target.log、mutation-summary.log、mut-*.log、restore-*.log、full.log、governance.log、all-golden-trade-audit.json、turtle-invariants.json；每个测试命令重定向并记录真实退出码。

## 现状表（30d6a07）

| 模板族 | 30d6a07 现状（provider 路径均省略 backtesting-py/） |
|---|---|
| 形态多空 | strategy_pattern_template.py:115-123 / 241-249：High/Low 触发，position.close() 下一根开盘 |
| 底部 | strategy_bottom_patterns.py:134-142：High/Low 触发，position.close() 下一根开盘 |
| 背离多空 | strategy_divergence.py:156-172：High/Low 触发，position.close() 下一根开盘 |
| 缠论多空 | strategy_chan.py:281-294：High/Low 触发，position.close() 下一根开盘 |
| F5 VWAP | cutie_backtesting_provider.py:6919-6939：默认 P1 原生；显式关闭时 Close 触发、下一根开盘 |
| F6 连阴 RSI | cutie_backtesting_provider.py:7094-7115：默认 P1 原生；显式关闭时 Close 触发、下一根开盘 |
| 斐波那契 | cutie_backtesting_provider.py:7509-7539：自带 High/Low 触发，position.close() 下一根开盘；用户关闭层 Close 判 |
| 海龟 | cutie_backtesting_provider.py:4425-4459：组 High/Low 触发，position.close() 下一根开盘 |

## 独立 review

实际模型 **claude-sonnet-5-5**。同一会话初始 12 轮额度耗尽（error_max_turns，不能当完成）；用同一 session 继续，只读工具 Read/Grep/Glob，不换模型，最终输出如下。review 未批准合入；发现未经返修，随候选保留。主助手已用当前代码加 advance spy 做本地合成 OHLC 核实：确认入场/止损发生在第 27 根，QUEUE_ADVANCE_BARS 不含 27；这证实队列漏推进一根，但尚未复现 reviewer 所述的延后入场后果。日志 confirm-reproduction.log，EXIT 0。

**审查结论：当前候选补丁是 draft PR，禁止合入。** 我没有发现需要立即回退的资金方向错误，但有一个未验证的真缺陷嫌疑和几项待裁定的契约变化。红门（扩展旧用例 23 failed、金样 EntryBar 51→24）不能算完成，我这里也没法证实或证伪。

## 模型与证据限制
- 实际模型：Sonnet 5.5（`claude-sonnet-5-5`）。
- 本会话 Bash 被禁用，我没有运行任何测试或 `git diff`。下面全部是静态阅读，没有复现，也没有核对你给的 "36 passed" 或 "23 failed / 238 passed"。
- 已读：`/tmp/psf-p2/candidate.diff` 全文；provider 的 `_FixedRiskMixin` 原生保护钩子、`_exit_kind_strategy`、`_final_close_strategy`、`_TurtleGroupMixin`、fib 和 vwap 构建段；五个模板文件；`PatternConfirmQueue`；backtesting.py 0.6.5 的 `_process_orders` 等。
- 未读：`tests/test_psf_p2_remaining_fills.py`、八个变异的内容、chan 的 `guarded_process_orders`。关于这些我没有结论。
- 金样 EntryBar 51→24 我没法从源码解释，不代替统领裁定。

## 真缺陷（静态判断，需用例复现）

### 1. 保护性出场根跳过整条 `next()` 链，confirm 队列漏掉一根 `advance`（中，嫌疑）
- 位置：`cutie_backtesting_provider.py:3589-3593`（`protected_next`）、`:2842-2844`（`PatternConfirmed.next`）、`strategy_entry_filters.py:219-220,243`。
- 原因：`protected_next` 包住的是整条链，包括 `PatternConfirmed.next`、`ExitKinds.next` 和 `FinalClose.next`。出场根上 `_pattern_confirm_advance()` 根本不会执行。
- 契约：队列文档要求 `advance` "once per bar in order"。
- 触发：`pattern_confirm_enabled=true`，且信号 B 在持仓或挂单期间已登记。登记条件是 `queue is not None` 时模板不检查 `self.orders`，所以单子挂着时也能登记。如果 B 的确认根（或窗口到期根）正好是入场根或持仓根的原生 SL/TP 出场根，这一根的确认和丢弃都被跳过。
- 后果：B 可能被推迟一根确认，用上一根已经过期的 level 在 k+2 入场；也可能不记 `unconfirmed` 或 `position_or_order_open`，让 `confirmed`、`discarded` 计数与逐根语义不一致。
- 影响面：在 P2 之前，原生钩子只在用户开风控层时生效。现在 `_template_native` 让所有 pattern、bottom、divergence、chan 的 confirm 运行都会走到这里；fib 和 vwap 在无显式定价时也是。
- fib 已补 `_risk_native_blocked_next`（`:7584`）只更新 `_fib_update`，没有推进队列，同样漏。
- 建议：出场根只阻止入场，不阻止 `advance`。例如把 `_risk_native_closed_bar` 放进 `_pattern_confirm_gate` 并让 `advance` 照常执行，让出场根上的确认走现有的 discard 原因；或者在 `blocked_next` 里调用 `_pattern_confirm_advance` 并 discard。
- 建议补一条用例：confirm 开启，入场根即止损，同根有待确认信号。

### 2. 报告里的同根优先级字符串过期（低到中）
- 位置：`cutie_backtesting_provider.py:11577`（fib）、`:11508`（turtle）、`:7256` 和 `:7322`（divergence 空、多）；`strategy_pattern_template.py:166-168` 的 docstring 也过期。
- 内容：这些字符串仍写 `stop_before_time_expiry_before_take_profit_…`。补丁后原生 TP 在 broker 内先于 `next()` 的 `max_holding` 成交。`_FixedRiskMixin` 的 docstring 自己写的是 "TP 优先"。
- 后果：`assumptions` 误报同根顺序。同一处 `:11576` 的 `default_trigger` 已经改了，说明这几处漏改。
- 建议：同步改字符串和 docstring。chan 的 assumptions 我没有查，请自行核对。

## 待统领裁定的行为差异（不是明确缺陷）

### 3. bottom 形态无条件入场，目标价已被跳过时会同根开平（低）
- 位置：`strategy_bottom_patterns.py:113-121`。
- 原因：`entry_open_at_or_above_frozen_target` 的跳过只在 `_pattern_confirm_queue is not None` 时才有。fib 的 `guarded_orders`（`:7481`）则是无条件跳过。
- 现象：关 confirm 时，如果 k+1 开盘价已不低于冻结目标（跳空），原生 TP 在入场根开盘价立刻成交，`ExitBar == EntryBar`，kind 为 take_profit。有点差时必亏。旧代码也入场，但下一根开盘才平。
- 建议：统一加跳过 guard。这会改变入场集合，要先过金样裁定。

### 4. 海龟金样 EntryBar 51→24（未解释）
- 位置：`cutie_backtesting_provider.py:4401-4442`（`_turtle_native_install`）、`:4482-4506`（删掉收盘判止损和止盈）。
- 预期变化：盘中止损和止盈更早平仓，会提前释放持仓，所以交易时间点变动是预期的。
- 不预期：27 根的跳变，同时 6 笔和 long/short 入场集合都不变。这无法用"早一根释放"解释。
- 建议：逐笔 dump 改前改后的 `_trades`（EntryBar、ExitBar、Size、reason），看是不是单位或分组顺序被重排，或者原来被挡的信号被放行。

## 已核对、未发现问题的点
- **入场根保护钩子**：`on_open`（`:3527`）先 `_risk_native_state` 再 `_risk_native_protect`。入场后同一 OHLC 再跑一次 `arbitrate`+`process`，入场根可触价止损。tp 先设、sl 后设，sl 通过 `insert(0)` 排在 tp 前，同根止损先。
- **跳空取开盘**：按 0.6.5 `:914-917`，SL 用 `min(open, stop)`，TP 用 `max(open, limit)`，符合预期。
- **parent 市价平仓**：非 contingent 的 parent close 在队首，`_filled_parent_order`（`:2903`）会让它先于同根 TP，且不会被标成保护性出场。`_risk_check_exit` 里的 `not order.is_contingent` 过滤也正确。
- **group-add 止损更新**：`protected_process`（`:4430-4440`）先跑 `process()`，再在 broker 阶段做 `_turtle_sync` 和 `protect()`，再跑一次 `process()`。旧止损先于加仓市价单；老止损或止盈命中时 `on_reduce` 取消待成交加仓。新止损覆盖全组，并在加仓根上可触发。
- **末根**：入场钩子和 `protect()` 在 `_at_final_bar` 时清掉 sl/tp，沿用 PIN7-E。末根保护性出场时 `protected_next` 跳过 `FinalClose.next`，导致 `_final_next_done` 不置位，但那时已无持仓，我没看到后果。
- **旧用户风控保持**：vwap、red_streak、fib 的 `_template_native` 在显式定价键存在时为 False。pattern、bottom、divergence、chan 本来就拒绝用户止损键，无条件为 True 没有问题。
- **exit_kind**：`stamp_fill` 在 `on_reduce` 之前，用 SL/TP 订单对象判定 kind，没发现标错。

## 预期契约变化
这些是补丁的目标，应该让旧断言跟着改，而不是视为缺陷：
- 模板自带止损和单一止盈，从"收盘判定、下一根开盘平"变成"原生盘中触价、跳空取开盘"。
- 同根单一 TP 先于 `max_holding` 到期。
- 出场根整根禁止再入场。
- 触发日志换成原生钩子写入（`_risk_native_record_exit`）。

扩展旧用例 23 个失败里，凡是断言"触发根+1、下一根开盘价"的，大概率属于这一类。但我没法逐条分类，建议逐条列出旧断言和新行为再确认。

## 建议的下一步
先写出发现 1 的复现用例，再裁定发现 3 和 4。发现 2 是文本同步，可以随时修。PR 保持 draft，绿门和金样重算完成前不要合入。

## 遗留与边界

待裁：海龟入场不变量；待做：review 确认问题返修、全量失败分类、元数据/README 成交与优先级口径同步、受影响金样按已有生成器重算并核入场与笔数、缺失的短向/多单加仓/确认队列边界验证。顶部（double_top/head_shoulders）共享相邻旧止损模式但本单未列，未迁移。分批 tp1-3、生产复算、部署、pin 均未做。本轮仅保存候选草稿。

## 全量失败清单

```text
FAILED tests/test_10b2a_sizing_div_fib_vwap.py::test_omitted_sizing_keys_match_c72c4a1_bytes[fibonacci_retracement-default]
FAILED tests/test_10b2a_sizing_div_fib_vwap.py::test_omitted_sizing_keys_match_c72c4a1_bytes[fibonacci_retracement-user_stop3]
FAILED tests/test_10b2a_sizing_div_fib_vwap.py::test_compound_uses_pre_fill_equity[fibonacci_retracement]
FAILED tests/test_10b2b_sizing_short_chan.py::test_omitted_sizing_keys_match_aa5f9e6_bytes[chan_3sell-default]
FAILED tests/test_10b2b_sizing_short_chan.py::test_omitted_sizing_keys_match_aa5f9e6_bytes[chan_3sell-leverage3]
FAILED tests/test_10b2b_sizing_short_chan.py::test_omitted_sizing_keys_match_aa5f9e6_bytes[chan_3buy-default]
FAILED tests/test_10b2c_sizing_kline_bottom.py::test_omitted_sizing_keys_match_5d0f9ac_bytes[morning_star-leverage2]
FAILED tests/test_10b2c_sizing_kline_bottom.py::test_omitted_sizing_keys_match_5d0f9ac_bytes[inside_bar_breakout-leverage3]
FAILED tests/test_10b2c_sizing_kline_bottom.py::test_omitted_sizing_keys_match_5d0f9ac_bytes[double_bottom-leverage2]
FAILED tests/test_10b2c_sizing_kline_bottom.py::test_omitted_sizing_keys_match_5d0f9ac_bytes[inverse_head_shoulders-leverage3]
FAILED tests/test_7p3b_pattern_range_filter.py::test_7p3b2_off_state_byte_identical_to_main[False-double_bottom/default]
FAILED tests/test_7p3b_pattern_range_filter.py::test_7p3b2_off_state_byte_identical_to_main[False-double_bottom/warm40]
FAILED tests/test_7p3b_pattern_range_filter.py::test_7p3b2_off_state_byte_identical_to_main[False-inverse_head_shoulders/default]
FAILED tests/test_7p3b_pattern_range_filter.py::test_7p3b2_off_state_byte_identical_to_main[False-inverse_head_shoulders/warm40]
FAILED tests/test_7p3b_pattern_range_filter.py::test_7p3b2_off_state_byte_identical_to_main[True-double_bottom/default]
FAILED tests/test_7p3b_pattern_range_filter.py::test_7p3b2_off_state_byte_identical_to_main[True-double_bottom/warm40]
FAILED tests/test_7p3b_pattern_range_filter.py::test_7p3b2_off_state_byte_identical_to_main[True-inverse_head_shoulders/default]
FAILED tests/test_7p3b_pattern_range_filter.py::test_7p3b2_off_state_byte_identical_to_main[True-inverse_head_shoulders/warm40]
FAILED tests/test_7p4_short_filter.py::test_short_templates_off_state_byte_identical_to_base[False-chan_3sell/default]
FAILED tests/test_7p4_short_filter.py::test_short_templates_off_state_byte_identical_to_base[False-chan_3sell/leverage3]
FAILED tests/test_7p4_short_filter.py::test_short_templates_off_state_byte_identical_to_base[False-chan_3sell/session]
FAILED tests/test_7p4_short_filter.py::test_short_templates_off_state_byte_identical_to_base[True-chan_3sell/default]
FAILED tests/test_7p4_short_filter.py::test_short_templates_off_state_byte_identical_to_base[True-chan_3sell/leverage3]
FAILED tests/test_7p4_short_filter.py::test_short_templates_off_state_byte_identical_to_base[True-chan_3sell/session]
FAILED tests/test_9t1_engulf_pin.py::test_r_target_from_actual_entry[True-2-bullish_engulfing]
FAILED tests/test_9t1_engulf_pin.py::test_r_target_from_actual_entry[True-2-hammer_pin_bar]
FAILED tests/test_9t1_engulf_pin.py::test_r_target_from_actual_entry[True-3-bullish_engulfing]
FAILED tests/test_9t1_engulf_pin.py::test_r_target_from_actual_entry[True-3-hammer_pin_bar]
FAILED tests/test_9t1_engulf_pin.py::test_buffered_frozen_stop[96.902-True-bullish_engulfing]
FAILED tests/test_9t1_engulf_pin.py::test_buffered_frozen_stop[96.902-True-hammer_pin_bar]
FAILED tests/test_9t1_engulf_pin.py::test_gap_skips_before_building_position[96.904-False-bullish_engulfing]
FAILED tests/test_9t1_engulf_pin.py::test_gap_skips_before_building_position[96.904-False-hammer_pin_bar]
FAILED tests/test_9t1_engulf_pin.py::test_time_decorator_gates_real_entries[bullish_engulfing]
FAILED tests/test_9t1_engulf_pin.py::test_time_decorator_gates_real_entries[hammer_pin_bar]
FAILED tests/test_9t1_engulf_pin.py::test_stop_priority_and_holding_expiry[bullish_engulfing]
FAILED tests/test_9t1_engulf_pin.py::test_stop_priority_and_holding_expiry[hammer_pin_bar]
FAILED tests/test_9t1_engulf_pin.py::test_frozen_off_http_bytes[False-False-bullish_engulfing]
FAILED tests/test_9t1_engulf_pin.py::test_frozen_off_http_bytes[False-False-hammer_pin_bar]
FAILED tests/test_9t1_engulf_pin.py::test_frozen_off_http_bytes[False-True-bullish_engulfing]
FAILED tests/test_9t1_engulf_pin.py::test_frozen_off_http_bytes[False-True-hammer_pin_bar]
FAILED tests/test_9t1_engulf_pin.py::test_frozen_off_http_bytes[True-False-bullish_engulfing]
FAILED tests/test_9t1_engulf_pin.py::test_frozen_off_http_bytes[True-False-hammer_pin_bar]
FAILED tests/test_9t1_engulf_pin.py::test_frozen_off_http_bytes[True-True-bullish_engulfing]
FAILED tests/test_9t1_engulf_pin.py::test_frozen_off_http_bytes[True-True-hammer_pin_bar]
FAILED tests/test_9t2_patterns.py::test_r_target_hand_computed_from_fill[2-True-morning_star]
FAILED tests/test_9t2_patterns.py::test_r_target_hand_computed_from_fill[2-True-three_white_soldiers]
FAILED tests/test_9t2_patterns.py::test_r_target_hand_computed_from_fill[2-True-bullish_doji_reversal]
FAILED tests/test_9t2_patterns.py::test_r_target_hand_computed_from_fill[2-True-inside_bar_breakout]
FAILED tests/test_9t2_patterns.py::test_r_target_hand_computed_from_fill[3-True-morning_star]
FAILED tests/test_9t2_patterns.py::test_r_target_hand_computed_from_fill[3-True-three_white_soldiers]
FAILED tests/test_9t2_patterns.py::test_r_target_hand_computed_from_fill[3-True-bullish_doji_reversal]
FAILED tests/test_9t2_patterns.py::test_r_target_hand_computed_from_fill[3-True-inside_bar_breakout]
FAILED tests/test_9t2_patterns.py::test_buffered_frozen_stop[96.902-True-morning_star]
FAILED tests/test_9t2_patterns.py::test_buffered_frozen_stop[96.902-True-three_white_soldiers]
FAILED tests/test_9t2_patterns.py::test_buffered_frozen_stop[96.902-True-bullish_doji_reversal]
FAILED tests/test_9t2_patterns.py::test_buffered_frozen_stop[96.902-True-inside_bar_breakout]
FAILED tests/test_9t2_patterns.py::test_stop_then_expiry_then_target[morning_star]
FAILED tests/test_9t2_patterns.py::test_stop_then_expiry_then_target[three_white_soldiers]
FAILED tests/test_9t2_patterns.py::test_stop_then_expiry_then_target[bullish_doji_reversal]
FAILED tests/test_9t2_patterns.py::test_stop_then_expiry_then_target[inside_bar_breakout]
FAILED tests/test_9t2_patterns.py::test_time_gate_entry_and_unrestricted_exit[morning_star]
FAILED tests/test_9t2_patterns.py::test_time_gate_entry_and_unrestricted_exit[three_white_soldiers]
FAILED tests/test_9t2_patterns.py::test_time_gate_entry_and_unrestricted_exit[bullish_doji_reversal]
FAILED tests/test_9t2_patterns.py::test_time_gate_entry_and_unrestricted_exit[inside_bar_breakout]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[False-False-morning_star]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[False-False-three_white_soldiers]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[False-False-bullish_doji_reversal]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[False-False-inside_bar_breakout]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[False-True-morning_star]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[False-True-three_white_soldiers]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[False-True-bullish_doji_reversal]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[False-True-inside_bar_breakout]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[True-False-morning_star]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[True-False-three_white_soldiers]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[True-False-bullish_doji_reversal]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[True-False-inside_bar_breakout]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[True-True-morning_star]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[True-True-three_white_soldiers]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[True-True-bullish_doji_reversal]
FAILED tests/test_9t2_patterns.py::test_frozen_off_http_bytes[True-True-inside_bar_breakout]
FAILED tests/test_9t2_patterns.py::test_inside_breakout_and_same_bar_inside_rearms_independently_of_filter[False]
FAILED tests/test_9t3_patterns.py::test_exit_priority_and_next_open_fill[stop_loss-double_bottom]
FAILED tests/test_9t3_patterns.py::test_exit_priority_and_next_open_fill[stop_loss-inverse_head_shoulders]
FAILED tests/test_9t3_patterns.py::test_exit_priority_and_next_open_fill[time_expiry-double_bottom]
FAILED tests/test_9t3_patterns.py::test_exit_priority_and_next_open_fill[time_expiry-inverse_head_shoulders]
FAILED tests/test_9t3_patterns.py::test_exit_priority_and_next_open_fill[take_profit-double_bottom]
FAILED tests/test_9t3_patterns.py::test_exit_priority_and_next_open_fill[take_profit-inverse_head_shoulders]
FAILED tests/test_9t3_patterns.py::test_off_state_v2_fingerprint[False-double_bottom]
FAILED tests/test_9t3_patterns.py::test_off_state_v2_fingerprint[False-inverse_head_shoulders]
FAILED tests/test_9t3_patterns.py::test_off_state_v2_fingerprint[True-double_bottom]
FAILED tests/test_9t3_patterns.py::test_off_state_v2_fingerprint[True-inverse_head_shoulders]
FAILED tests/test_9t4_divergence.py::test_2r_uses_actual_fill_and_remains_frozen[macd_bullish_divergence]
FAILED tests/test_9t4_divergence.py::test_2r_uses_actual_fill_and_remains_frozen[rsi_bullish_divergence]
FAILED tests/test_9t4_divergence.py::test_same_bar_exit_priority[True-True-stop_loss-macd_bullish_divergence]
FAILED tests/test_9t4_divergence.py::test_same_bar_exit_priority[True-True-stop_loss-rsi_bullish_divergence]
FAILED tests/test_9t4_divergence.py::test_same_bar_exit_priority[False-True-time_expiry-macd_bullish_divergence]
FAILED tests/test_9t4_divergence.py::test_same_bar_exit_priority[False-True-time_expiry-rsi_bullish_divergence]
FAILED tests/test_9t4_divergence.py::test_same_bar_exit_priority[False-False-take_profit-macd_bullish_divergence]
FAILED tests/test_9t4_divergence.py::test_same_bar_exit_priority[False-False-take_profit-rsi_bullish_divergence]
FAILED tests/test_9t4_divergence.py::test_time_flatten_beats_target[macd_bullish_divergence]
FAILED tests/test_9t4_divergence.py::test_time_flatten_beats_target[rsi_bullish_divergence]
FAILED tests/test_9t5_fibonacci.py::test_touch_exit_priority_and_next_open_fill[103-121-1-stop_loss]
FAILED tests/test_9t5_fibonacci.py::test_touch_exit_priority_and_next_open_fill[106-121-1-time_expiry]
FAILED tests/test_9t5_fibonacci.py::test_touch_exit_priority_and_next_open_fill[106-121-0-take_profit]
FAILED tests/test_9t5_fibonacci.py::test_intrinsic_defaults_touch_even_with_risk_layer_off
FAILED tests/test_9t5_fibonacci.py::test_same_pair_cannot_enter_twice - asser...
FAILED tests/test_9t5_fibonacci.py::test_registered_http_report_and_frozen_result_v2
FAILED tests/test_9t5_fibonacci.py::test_position_keeps_old_target_when_a_new_pair_confirms
FAILED tests/test_9t6_chan_3buy.py::test_next_open_and_actual_fill_frozen_r
FAILED tests/test_9t6_chan_3buy.py::test_exit_priority[False-True-True-time_expiry]
FAILED tests/test_9t6_chan_3buy.py::test_warmup_points_allow_main_range_entry_without_warmup_orders
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-double_bottom]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-double_bottom@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-inverse_head_shoulders]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-inverse_head_shoulders@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-bullish_engulfing]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-bullish_engulfing@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-hammer_pin_bar]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-hammer_pin_bar@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-morning_star]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-morning_star@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-three_white_soldiers]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-three_white_soldiers@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-bullish_doji_reversal]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-bullish_doji_reversal@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-inside_bar_breakout]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-inside_bar_breakout@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-rsi_bullish_divergence]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-rsi_bullish_divergence@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-chan_3buy]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-chan_3buy@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-fibonacci_retracement]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-False-fibonacci_retracement@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-double_bottom]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-double_bottom@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-inverse_head_shoulders]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-inverse_head_shoulders@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-bullish_engulfing]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-bullish_engulfing@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-hammer_pin_bar]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-hammer_pin_bar@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-morning_star]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-morning_star@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-three_white_soldiers]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-three_white_soldiers@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-bullish_doji_reversal]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-bullish_doji_reversal@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-inside_bar_breakout]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-inside_bar_breakout@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-rsi_bullish_divergence]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-rsi_bullish_divergence@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-chan_3buy]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-chan_3buy@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-fibonacci_retracement]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[False-True-fibonacci_retracement@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-double_bottom]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-double_bottom@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-inverse_head_shoulders]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-inverse_head_shoulders@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-bullish_engulfing]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-bullish_engulfing@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-hammer_pin_bar]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-hammer_pin_bar@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-morning_star]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-morning_star@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-three_white_soldiers]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-three_white_soldiers@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-bullish_doji_reversal]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-bullish_doji_reversal@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-inside_bar_breakout]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-inside_bar_breakout@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-rsi_bullish_divergence]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-rsi_bullish_divergence@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-chan_3buy]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-chan_3buy@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-fibonacci_retracement]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-False-fibonacci_retracement@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-double_bottom]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-double_bottom@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-inverse_head_shoulders]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-inverse_head_shoulders@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-bullish_engulfing]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-bullish_engulfing@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-hammer_pin_bar]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-hammer_pin_bar@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-morning_star]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-morning_star@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-three_white_soldiers]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-three_white_soldiers@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-bullish_doji_reversal]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-bullish_doji_reversal@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-inside_bar_breakout]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-inside_bar_breakout@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-rsi_bullish_divergence]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-rsi_bullish_divergence@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-chan_3buy]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-chan_3buy@long]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-fibonacci_retracement]
FAILED tests/test_entry_filters.py::test_runtime_disabled_fingerprints[True-True-fibonacci_retracement@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-double_bottom]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-double_bottom@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-inverse_head_shoulders]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-inverse_head_shoulders@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-bullish_engulfing]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-bullish_engulfing@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-hammer_pin_bar]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-hammer_pin_bar@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-morning_star]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-morning_star@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-three_white_soldiers]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-three_white_soldiers@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-bullish_doji_reversal]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-bullish_doji_reversal@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-inside_bar_breakout]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-inside_bar_breakout@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-rsi_bullish_divergence]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-rsi_bullish_divergence@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-chan_3buy]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-chan_3buy@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-fibonacci_retracement]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[False-fibonacci_retracement@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-double_bottom]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-double_bottom@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-inverse_head_shoulders]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-inverse_head_shoulders@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-bullish_engulfing]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-bullish_engulfing@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-hammer_pin_bar]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-hammer_pin_bar@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-morning_star]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-morning_star@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-three_white_soldiers]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-three_white_soldiers@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-bullish_doji_reversal]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-bullish_doji_reversal@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-inside_bar_breakout]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-inside_bar_breakout@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-rsi_bullish_divergence]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-rsi_bullish_divergence@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-chan_3buy]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-chan_3buy@long]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-fibonacci_retracement]
FAILED tests/test_entry_filters_mtf.py::test_explicit_default_timeframe_preserves_frozen_off_bytes[True-fibonacci_retracement@long]
FAILED tests/test_f5_vwap_reversion.py::test_two_day_hand_vwap_and_next_open_fills
FAILED tests/test_isolated_liquidation_arbitration.py::test_runtime_mixin_off_state_bytes[default-bullish_doji_reversal]
FAILED tests/test_isolated_liquidation_arbitration.py::test_runtime_mixin_off_state_bytes[default-chan_3buy]
FAILED tests/test_isolated_liquidation_arbitration.py::test_runtime_mixin_off_state_bytes[default-fibonacci_retracement]
FAILED tests/test_isolated_liquidation_arbitration.py::test_runtime_mixin_off_state_bytes[leverage_one-bullish_doji_reversal]
FAILED tests/test_isolated_liquidation_arbitration.py::test_runtime_mixin_off_state_bytes[leverage_one-chan_3buy]
FAILED tests/test_isolated_liquidation_arbitration.py::test_runtime_mixin_off_state_bytes[leverage_one-fibonacci_retracement]
FAILED tests/test_isolated_liquidation_arbitration.py::test_runtime_mixin_off_state_bytes[spot-bullish_doji_reversal]
FAILED tests/test_isolated_liquidation_arbitration.py::test_runtime_mixin_off_state_bytes[spot-chan_3buy]
FAILED tests/test_isolated_liquidation_arbitration.py::test_runtime_mixin_off_state_bytes[spot-fibonacci_retracement]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[off/bearish_doji_reversal]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[off/bullish_doji_reversal]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[off/chan_3buy]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[off/chan_3sell]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[off/fibonacci_retracement]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[on/bearish_doji_reversal]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[on/bullish_doji_reversal]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[on/chan_3buy]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[on/chan_3sell]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[on/fibonacci_retracement]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[traded/chan_3buy]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[traded/chan_3sell]
FAILED tests/test_patconf1_pattern_confirm.py::test_omitted_keys_byte_identical_to_5ddd8eb[traded/fibonacci_retracement]
FAILED tests/test_patconf2b1_patterns.py::test_bullish_engulfing_hand_computed
FAILED tests/test_patconf2b1_patterns.py::test_three_black_crows_hand_computed
FAILED tests/test_patconf2b1_patterns.py::test_double_bottom_hand_computed - ...
FAILED tests/test_patconf2b1_patterns.py::test_open_position_at_k_discards_the_confirmed_signal
FAILED tests/test_patconf2b1_patterns.py::test_placed_random_signals_discard_at_an_open_position_without_a_second_order[bearish_doji_reversal-2]
FAILED tests/test_patconf2b1_patterns.py::test_placed_random_signals_discard_at_an_open_position_without_a_second_order[bullish_engulfing-3]
FAILED tests/test_patconf2b1_patterns.py::test_placed_random_signals_discard_at_an_open_position_without_a_second_order[evening_star-2]
FAILED tests/test_patconf2b1_patterns.py::test_placed_random_signals_discard_at_an_open_position_without_a_second_order[hammer_pin_bar-3]
FAILED tests/test_patconf2b1_patterns.py::test_placed_random_signals_discard_at_an_open_position_without_a_second_order[inside_bar_breakout-3]
FAILED tests/test_patconf2b1_patterns.py::test_placed_random_signals_discard_at_an_open_position_without_a_second_order[three_black_crows-3]
FAILED tests/test_patconf2b1_patterns.py::test_placed_random_signals_discard_at_an_open_position_without_a_second_order[three_white_soldiers-1]
FAILED tests/test_patconf2b1_patterns.py::test_placed_random_runs_do_hit_open_positions
FAILED tests/test_patconf2b2_more.py::test_rsi_bullish_divergence_hand_computed
FAILED tests/test_patconf2b2_more.py::test_chan_3sell_hand_computed - Asserti...
FAILED tests/test_patconf2b2_more.py::test_fibonacci_retracement_hand_computed
FAILED tests/test_patconf2b2_more.py::test_fibonacci_confirmation_discards_a_wave_already_used
FAILED tests/test_pin7e_final_close.py::test_template_stop_cannot_veto_last_bar_liquidation[bullish_engulfing]
FAILED tests/test_pin7e_final_close.py::test_template_stop_cannot_veto_last_bar_liquidation[bearish_engulfing]
FAILED tests/test_pin7e_final_close.py::test_template_stop_cannot_veto_last_bar_liquidation[double_bottom]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[bullish_engulfing-A1-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[bullish_engulfing-A1-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[bullish_engulfing-A2-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[bullish_engulfing-A2-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[hammer_pin_bar-A1-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[hammer_pin_bar-A1-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[hammer_pin_bar-A2-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[hammer_pin_bar-A2-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[morning_star-A1-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[morning_star-A1-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[morning_star-A2-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[morning_star-A2-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[three_white_soldiers-A1-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[three_white_soldiers-A1-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[three_white_soldiers-A2-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[three_white_soldiers-A2-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[bullish_doji_reversal-A1-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[bullish_doji_reversal-A1-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[bullish_doji_reversal-A2-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[bullish_doji_reversal-A2-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[inside_bar_breakout-A1-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[inside_bar_breakout-A1-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[inside_bar_breakout-A2-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[inside_bar_breakout-A2-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[double_bottom-A1-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[double_bottom-A1-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[double_bottom-A2-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[double_bottom-A2-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[inverse_head_shoulders-A1-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[inverse_head_shoulders-A1-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[inverse_head_shoulders-A2-full]
FAILED tests/test_pliq1_pattern_liquidation.py::test_scenario_matrix_follows_t2_2b[inverse_head_shoulders-A2-pct20]
FAILED tests/test_pliq1_pattern_liquidation.py::test_insolvency_bridge_passes_frozen_stop[bullish_engulfing]
FAILED tests/test_pliq1_pattern_liquidation.py::test_insolvency_bridge_passes_frozen_stop[hammer_pin_bar]
FAILED tests/test_pliq1_pattern_liquidation.py::test_insolvency_bridge_passes_frozen_stop[morning_star]
FAILED tests/test_pliq1_pattern_liquidation.py::test_insolvency_bridge_passes_frozen_stop[three_white_soldiers]
FAILED tests/test_pliq1_pattern_liquidation.py::test_insolvency_bridge_passes_frozen_stop[bullish_doji_reversal]
FAILED tests/test_pliq1_pattern_liquidation.py::test_insolvency_bridge_passes_frozen_stop[inside_bar_breakout]
FAILED tests/test_pliq1_pattern_liquidation.py::test_insolvency_bridge_passes_frozen_stop[double_bottom]
FAILED tests/test_pliq1_pattern_liquidation.py::test_insolvency_bridge_passes_frozen_stop[inverse_head_shoulders]
FAILED tests/test_plow2a_filter_warmup_gap.py::test_filter_on_contiguous_warmup_byte_identical_to_base[bullish_doji_reversal]
FAILED tests/test_plow2a_filter_warmup_gap.py::test_filter_on_contiguous_warmup_byte_identical_to_base[chan_3buy]
FAILED tests/test_plow2a_filter_warmup_gap.py::test_filter_on_contiguous_warmup_byte_identical_to_base[chan_3sell]
FAILED tests/test_plow2a_filter_warmup_gap.py::test_filter_on_contiguous_warmup_byte_identical_to_base[fibonacci_retracement]
FAILED tests/test_plow2a_filter_warmup_gap.py::test_filter_off_gapped_warmup_byte_identical_to_base[bullish_doji_reversal]
FAILED tests/test_plow2a_filter_warmup_gap.py::test_filter_off_gapped_warmup_byte_identical_to_base[chan_3buy]
FAILED tests/test_plow2a_filter_warmup_gap.py::test_filter_off_gapped_warmup_byte_identical_to_base[chan_3sell]
FAILED tests/test_plow2a_filter_warmup_gap.py::test_filter_off_gapped_warmup_byte_identical_to_base[fibonacci_retracement]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[bearish_doji_reversal]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[bearish_engulfing]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[bullish_doji_reversal]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[bullish_engulfing]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[chan_3buy]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[chan_3sell]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[double_bottom]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[evening_star]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[fibonacci_retracement]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[hammer_pin_bar]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[inside_bar_breakout]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[inverse_head_shoulders]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[macd_bearish_divergence]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[macd_bullish_divergence]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[morning_star]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[rsi_bearish_divergence]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[rsi_bullish_divergence]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[shooting_star]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[three_black_crows]
FAILED tests/test_plow2b.py::test_sizing_initial_stop_is_the_stop_the_exit_fires_on[three_white_soldiers]
FAILED tests/test_q17b_patconf_guards.py::test_off_response_bytes_match_pin3[False-chan_3buy]
FAILED tests/test_q17b_patconf_guards.py::test_off_response_bytes_match_pin3[False-chan_3sell]
FAILED tests/test_q17b_patconf_guards.py::test_off_response_bytes_match_pin3[False-fibonacci_retracement]
FAILED tests/test_q17b_patconf_guards.py::test_off_response_bytes_match_pin3[True-chan_3buy]
FAILED tests/test_q17b_patconf_guards.py::test_off_response_bytes_match_pin3[True-chan_3sell]
FAILED tests/test_q17b_patconf_guards.py::test_off_response_bytes_match_pin3[True-fibonacci_retracement]
FAILED tests/test_q17b_patconf_guards.py::test_off_gap_response_bytes_match_pin3[double_bottom]
FAILED tests/test_q17b_patconf_guards.py::test_off_gap_response_bytes_match_pin3[inverse_head_shoulders]
FAILED tests/test_q17b_patconf_guards.py::test_pi_double_bottom_target_overshoot_exact_prices
FAILED tests/test_q42c_risk_layer_default.py::test_red_streak_rsi_default_group_runs_unified_layer
FAILED tests/test_q42c_risk_layer_default.py::test_vwap_reversion_default_stop_preempts_day_end_flatten
FAILED tests/test_risk_overlay_compatibility.py::test_turtle_disabled_group_risk_exemption_preserves_golden[long-turtle]
FAILED tests/test_risk_overlay_compatibility.py::test_turtle_disabled_group_risk_exemption_preserves_golden[short-turtle]
FAILED tests/test_risk_overlay_compatibility.py::test_turtle_disabled_group_risk_exemption_preserves_golden[both-turtle]
FAILED tests/test_short_pat1.py::test_long_source_and_golden_files_byte_unchanged
FAILED tests/test_short_pat2.py::test_2r_uses_actual_fill_and_remains_frozen[macd_bearish_divergence]
FAILED tests/test_short_pat2.py::test_2r_uses_actual_fill_and_remains_frozen[rsi_bearish_divergence]
FAILED tests/test_short_pat2.py::test_same_bar_exit_priority[True-True-stop_loss-macd_bearish_divergence]
FAILED tests/test_short_pat2.py::test_same_bar_exit_priority[True-True-stop_loss-rsi_bearish_divergence]
FAILED tests/test_short_pat2.py::test_same_bar_exit_priority[False-True-time_expiry-macd_bearish_divergence]
FAILED tests/test_short_pat2.py::test_same_bar_exit_priority[False-True-time_expiry-rsi_bearish_divergence]
FAILED tests/test_short_pat2.py::test_same_bar_exit_priority[False-False-take_profit-macd_bearish_divergence]
FAILED tests/test_short_pat2.py::test_same_bar_exit_priority[False-False-take_profit-rsi_bearish_divergence]
FAILED tests/test_short_pat2.py::test_time_flatten_beats_target[macd_bearish_divergence]
FAILED tests/test_short_pat2.py::test_time_flatten_beats_target[rsi_bearish_divergence]
FAILED tests/test_short_pat2.py::test_short_isolated_risk_arbitration_and_accounting[closer_stop-macd_bearish_divergence]
FAILED tests/test_short_pat2.py::test_short_isolated_risk_arbitration_and_accounting[closer_stop-rsi_bearish_divergence]
FAILED tests/test_short_pat3_chan_3sell.py::test_next_open_and_actual_fill_frozen_r
FAILED tests/test_short_pat3_chan_3sell.py::test_exit_priority[False-True-True-time_expiry]
FAILED tests/test_short_pat3_chan_3sell.py::test_warmup_points_allow_main_range_entry
FAILED tests/test_short_pat3_chan_3sell.py::test_short_isolated_risk_arbitration_and_accounting[closer_stop]
FAILED tests/test_short_pat3_chan_3sell.py::test_branch_head_isolated_off_golden[False-0]
FAILED tests/test_short_pat3_chan_3sell.py::test_branch_head_isolated_off_golden[False-10]
FAILED tests/test_short_pat3_chan_3sell.py::test_branch_head_isolated_off_golden[True-0]
FAILED tests/test_short_pat3_chan_3sell.py::test_branch_head_isolated_off_golden[True-10]
FAILED tests/test_short_pat3_chan_3sell.py::test_chan_3buy_response_bytes_match_original_engine[0-spot-params0]
FAILED tests/test_short_pat3_chan_3sell.py::test_chan_3buy_response_bytes_match_original_engine[0-futures-params1]
FAILED tests/test_short_pat3_chan_3sell.py::test_chan_3buy_response_bytes_match_original_engine[0-futures-params3]
FAILED tests/test_short_pat3_chan_3sell.py::test_chan_3buy_response_bytes_match_original_engine[10-spot-params0]
FAILED tests/test_short_pat3_chan_3sell.py::test_chan_3buy_response_bytes_match_original_engine[10-futures-params1]
FAILED tests/test_short_pat3_chan_3sell.py::test_chan_3buy_response_bytes_match_original_engine[10-futures-params3]
FAILED tests/test_short_pat4_candles.py::test_frozen_stop_above_entry_exits_next_open[bearish_engulfing]
FAILED tests/test_short_pat4_candles.py::test_frozen_stop_above_entry_exits_next_open[shooting_star]
FAILED tests/test_short_pat4_candles.py::test_frozen_stop_above_entry_exits_next_open[evening_star]
FAILED tests/test_short_pat4_candles.py::test_frozen_stop_above_entry_exits_next_open[three_black_crows]
FAILED tests/test_short_pat4_candles.py::test_frozen_stop_above_entry_exits_next_open[bearish_doji_reversal]
FAILED tests/test_short_pat4_candles.py::test_target_below_entry_by_reward_r_times_risk[bearish_engulfing-2]
FAILED tests/test_short_pat4_candles.py::test_target_below_entry_by_reward_r_times_risk[bearish_engulfing-0.5]
FAILED tests/test_short_pat4_candles.py::test_target_below_entry_by_reward_r_times_risk[shooting_star-2]
FAILED tests/test_short_pat4_candles.py::test_target_below_entry_by_reward_r_times_risk[shooting_star-0.5]
FAILED tests/test_short_pat4_candles.py::test_target_below_entry_by_reward_r_times_risk[evening_star-2]
FAILED tests/test_short_pat4_candles.py::test_target_below_entry_by_reward_r_times_risk[evening_star-0.5]
FAILED tests/test_short_pat4_candles.py::test_target_below_entry_by_reward_r_times_risk[three_black_crows-2]
FAILED tests/test_short_pat4_candles.py::test_target_below_entry_by_reward_r_times_risk[three_black_crows-0.5]
FAILED tests/test_short_pat4_candles.py::test_target_below_entry_by_reward_r_times_risk[bearish_doji_reversal-2]
FAILED tests/test_short_pat4_candles.py::test_target_below_entry_by_reward_r_times_risk[bearish_doji_reversal-0.5]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[bearish_engulfing-A1-full]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[bearish_engulfing-A1-pct20]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[bearish_engulfing-A2-full]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[bearish_engulfing-A2-pct20]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[shooting_star-A1-full]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[shooting_star-A1-pct20]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[shooting_star-A2-full]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[shooting_star-A2-pct20]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[evening_star-A1-full]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[evening_star-A1-pct20]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[evening_star-A2-full]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[evening_star-A2-pct20]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[three_black_crows-A1-full]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[three_black_crows-A1-pct20]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[three_black_crows-A2-full]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[three_black_crows-A2-pct20]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[bearish_doji_reversal-A1-full]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[bearish_doji_reversal-A1-pct20]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[bearish_doji_reversal-A2-full]
FAILED tests/test_short_pat4_candles.py::test_isolated_scenarios_follow_t2_2b[bearish_doji_reversal-A2-pct20]
FAILED tests/test_time_layer_6b.py::test_every_registered_single_template_consumes_holding_bars[chan_3buy]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-double_bottom]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-inverse_head_shoulders]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-bullish_engulfing]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-hammer_pin_bar]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-morning_star]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-three_white_soldiers]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-bullish_doji_reversal]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-inside_bar_breakout]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-chan_3buy]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-chan_3sell]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[False-fibonacci_retracement]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-double_bottom]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-inverse_head_shoulders]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-bullish_engulfing]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-hammer_pin_bar]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-morning_star]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-three_white_soldiers]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-bullish_doji_reversal]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-inside_bar_breakout]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-chan_3buy]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-chan_3sell]
FAILED tests/test_time_layer_compatibility.py::test_disabled_response_and_start_unchanged[True-fibonacci_retracement]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-inside_bar_breakout]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-bearish_doji_reversal]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-three_white_soldiers]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-inverse_head_shoulders]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-chan_3buy]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-bearish_engulfing]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-bullish_engulfing]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-three_black_crows]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-hammer_pin_bar]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-evening_star]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-morning_star]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-shooting_star]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-bullish_doji_reversal]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-double_bottom]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[False-chan_3sell]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-inside_bar_breakout]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-bearish_doji_reversal]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-three_white_soldiers]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-inverse_head_shoulders]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-chan_3buy]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-bearish_engulfing]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-bullish_engulfing]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-three_black_crows]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-hammer_pin_bar]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-evening_star]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-morning_star]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-shooting_star]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-bullish_doji_reversal]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-double_bottom]
FAILED tests/test_time_layer_compatibility.py::test_each_template_gates_entries_but_allows_outside_exits[True-chan_3sell]
FAILED tests/test_time_layer_compatibility.py::test_turtle_group_time_wired_preserves_disabled_golden[long-turtle]
FAILED tests/test_time_layer_compatibility.py::test_turtle_group_time_wired_preserves_disabled_golden[short-turtle]
FAILED tests/test_time_layer_compatibility.py::test_turtle_group_time_wired_preserves_disabled_golden[both-turtle]
FAILED tests/test_turtle_nstop.py::test_group_n_and_stop_follow_last_fill[extra0-both]
FAILED tests/test_turtle_nstop.py::test_group_n_and_stop_follow_last_fill[extra1-both]
FAILED tests/test_turtle_nstop.py::test_plain_turtle_also_emits_n_and_stop_matching_hand[both]
FAILED tests/test_turtle_risk_3b.py::test_add_does_not_reset_group_expiry_and_due_prevents_add[long]
FAILED tests/test_turtle_risk_3b.py::test_add_does_not_reset_group_expiry_and_due_prevents_add[short]
FAILED tests/test_turtle_risk_3b.py::test_add_does_not_reset_group_expiry_and_due_prevents_add[both]
FAILED tests/test_turtle_risk_3b.py::test_take_profit_scalar_vwap_and_next_open[False-long]
FAILED tests/test_turtle_risk_3b.py::test_take_profit_scalar_vwap_and_next_open[False-short]
FAILED tests/test_turtle_risk_3b.py::test_take_profit_scalar_vwap_and_next_open[True-long]
FAILED tests/test_turtle_risk_3b.py::test_take_profit_scalar_vwap_and_next_open[True-short]
FAILED tests/test_turtle_risk_3b.py::test_exit_arbitration[long-stop_expiry-stop]
FAILED tests/test_turtle_risk_3b.py::test_exit_arbitration[long-expiry_take-time_expiry]
FAILED tests/test_turtle_risk_3b.py::test_exit_arbitration[long-take_channel-take_profit]
FAILED tests/test_turtle_risk_3b.py::test_exit_arbitration[short-stop_expiry-stop]
FAILED tests/test_turtle_risk_3b.py::test_exit_arbitration[short-expiry_take-time_expiry]
FAILED tests/test_turtle_risk_3b.py::test_exit_arbitration[short-take_channel-take_profit]
FAILED tests/test_turtle_template.py::test_golden_engine_times_prices_quantities_and_group_ids
FAILED tests/test_turtle_template.py::test_golden_fill_baseline_one_add_per_bar_frozen_n_and_raised_stop
FAILED tests/test_turtle_template.py::test_registered_route_exclusivity_tags_and_assumptions[local.backtesting_py.turtle]
FAILED tests/test_turtle_template.py::test_stop_precedes_add_even_if_close_is_favorable
FAILED tests/test_turtle_template.py::test_exit_fill_bar_cannot_start_another_group
FAILED tests/test_turtle_template.py::test_directional_engine_golden_and_unique_position_direction[short]
FAILED tests/test_turtle_template.py::test_directional_engine_golden_and_unique_position_direction[both]
FAILED tests/test_turtle_template.py::test_futures_direction_registered_route[short]
FAILED tests/test_turtle_template.py::test_futures_direction_registered_route[both]
FAILED tests/test_turtle_template.py::test_short_stop_uses_high_not_low - ass...
FAILED tests/test_turtle_template.py::test_short_add_uses_actual_fill_frozen_n_and_one_per_bar
FAILED tests/test_turtle_template.py::test_both_reverse_signal_on_exit_fill_bar_is_blocked
FAILED tests/test_turtle_time_layer.py::test_omitted_time_keys_response_byte_identical[both]
FAILED tests/test_turtle_time_layer.py::test_omitted_time_keys_response_byte_identical[long]
FAILED tests/test_turtle_time_layer.py::test_omitted_time_keys_response_byte_identical[long_risk_layer]
FAILED tests/test_turtle_time_layer.py::test_omitted_time_keys_response_byte_identical[short]
FAILED tests/test_turtle_time_layer.py::test_same_bar_group_stop_beats_time_expiry
```
