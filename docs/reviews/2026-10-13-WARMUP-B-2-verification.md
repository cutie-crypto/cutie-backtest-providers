# WARMUP-B-2 验证与停止边界

接管已授权的codex-2补丁；fetch确认main=678b4e1，rebase已无冲突完成，原e2b1565与562c627的patch-id一致。主checkout未改。

- 原2895 failed/6 errors：初始8类候选见/tmp/warmup-b2/root-clusters.tsv；最新main同命令10646 passed/EXIT=0。旧健康门红由main PR99收口，本单不修基线问题。
- 收尾命令（仓根）：python3 -m pytest -q backtesting-py/tests --junitxml=/tmp/warmup-b2/full.xml > /tmp/warmup-b2/full.log 2>&1; echo "EXIT=$?"。接管前中断日志另存full-aborted-codex2.log，不冒充完成全量。
- 51个金样、235组响应对main核对：入场根/价格/笔数/方向变化0；trades全字段、equity_curve、metrics、data_manifest变化0。读取原JSON与现成post/snapshot/canonical返回做比较，通过才写出；未手改JSON。
- 单文件最终快信号：10b2a=30、10b2b=41、10b2c=51、pliq1=171、event0=57、scenarios=4、template=32、explicit=22、macro=171，均passed/EXIT=0。2d整文件27 passed/4 failed，red_streak_rsi旧冷路径待做。
- 冻结空种子捷径绕过校验变异：test_zero_budget_frozen_seed_replays_before_history_shortcut 1 failed/EXIT=1；cp还原/cmp相同，explicit全文件22 passed/EXIT=0。继承零预算删除分支变异在mutation-zero.log另存。
- patconf2b1的6组、P6的10个响应出现入场变化，停止文件并cp撤回本次文件自身尝试；不修改判据与金样。详细旧/新在blocked-patconf-entry-identities.json、p6-entry-changes.json。
- Sonnet5.5有效增量review追加原2026-10-13-WARMUP-B-review.md；源编译及diff --check EXIT=0。
- 高周期完整冻结复放、1M非固定网格待做，明确不在本单。无main合入，无pin/部署/实盘行情取证。
- 最终全量：8682 passed、1987 failed、0 errors、EXIT=1；最新main同命令10646 passed、0失败/错误、EXIT=0，因此新增失败1987，基线红0，未达到清零；PR98保持draft。
- 全量仅在本单收尾完成一次，源测试已冻结；接管前中断运行另存日志，不算绿。
- 原PR历史通过树不变merge连接（604a979），原rebase前后patch-id一致，普通push，无force。

## 收尾失败聚类（机械候选，未完成逐条因果验收）

- 完整响应/指标金样未适配（待核） · 649 · backtesting-py/tests/test_10b2d_sizing_range_calendar.py::test_omitted_sizing_keys_match_1f5b8dc_bytes[red_streak_rsi-default]
- 新严格拒跑与旧HTTP成功期望冲突（待核） · 728 · backtesting-py/tests/test_10b2d_sizing_range_calendar.py::test_risk_quantity_hand_calculated[red_streak_default]
- 过滤/高周期完整前缀不足或旧闭合断言 · 22 · backtesting-py/tests/test_10b_risk_sizing.py::test_http_registered_route_quantity_and_report[params0-0-50-None]
- 其它旧fixture或指标断言（待逐例核实） · 188 · backtesting-py/tests/test_7p3b_pattern_range_filter.py::test_off_state_byte_identical_to_main[False-default/hand]
- 旧预算/起止/缺口判据未适配（待核） · 177 · backtesting-py/tests/test_7p3b_pattern_range_filter.py::test_filter_warmup_short_history_fails_closed
- 入场变化停止件，保留未适配桩 · 223 · backtesting-py/tests/test_p6_trade_exit_kinds.py::test_every_v2_tool_reports_one_kind_per_trade[adx_di_cross]

完整1987条归因：/tmp/warmup-b2/full-failure-attribution.tsv；计数/基线对照：full-comparison.json。未把剩余失败包装成通过或xfail。

## 停止文件示例

- test_patconf2b1_patterns.py::test_placed_random_signals_discard_at_an_open_position_without_a_second_order[double_bottom-1]：旧3笔（首根1767319200、104.71785812661739、long），新2笔（首根1767294000、103.7764943559087、long）；总6组，文件自身尝试已cp撤回。
- test_p6_trade_exit_kinds.py::test_every_v2_tool_reports_one_kind_per_trade[adx_di_cross]：旧14笔、首根1704211200；新15笔、首根1704160800；总10个响应，文件自身尝试已cp撤回。
- 两件本单外遗留：高周期完整冻结复放（provider冻结请求入口）；1M非固定网格（_timeframe_milliseconds等固定间隔假设）待另单，不在本批补实现。
