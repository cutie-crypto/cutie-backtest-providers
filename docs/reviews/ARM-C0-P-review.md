# ARM-C0-P 异模型审查

模型：Claude Sonnet 5.5，实际 canonicalModel=`claude-sonnet-5-5`、provider=`firstParty`；只读 CLI 运行 EXIT=0。范围：main@0e8b0ce 至本工作树完整生产和测试 diff。首次因材料目录未授权没有完成审查；补齐只读目录后下列审查完成。

**审查结论：生产代码没有发现会让回测结果错误的 finding，有 1 个 Medium（1h 清算报告体积）和 3 个 Low/Info。** 范围是 `origin/main=0e8b0ce` 到当前工作树的生产 diff，已对照 `cutie_backtesting_provider.py` 的 `_fetch_metric_series`/`_assert_contiguous`/两个 bind、两个 strategy 全文和 macro 引擎源码。

**Finding**
1. **Medium：1h 清算回测的窗口被报告体积上限卡住。**
   - 触发：1h 下每小时一条事件。`strategy_liquidation_reversal.py:346-350` 每小时 append 一条 `macro_events`，`strategy_macro_events.py:357-363` 的 `macro_report` 又把同一批事件放进 `events` 和 `skipped_events` 两份。
   - 按金样日线记录约 330B、加 `skipped_events` 约 100B 估算，每小时约 450B。`_bounded_template_response`（`cutie_backtesting_provider.py:12059-12071`）的 raw_report 上限是 1MB，所以 1h 窗口超过约 95–100 天（约 2300 根）就会返回 INVALID_PARAMS「shorten the backtest range」。
   - 影响：fail-closed，不涉及钱路径，但长窗口的 1h 清算模板无法完成。用例 ⑤ 的注释（`test_arm_c0_p_hourly_metrics.py:581-582`）自己承认这点，并绕开完整响应路径，所以全路径下的体积上限没有用例覆盖。
   - 建议：统领确认是否接受这个窗口上限；若不接受，需要在小时流下裁剪事件记录。
2. **Low：两模板的判定时点差一根 K 线。**
   - top：H 值在 H 根收盘判，H+1 开盘成交（`strategy_top_long_short_reversal.py:138-142`）。
   - 清算：H 值在 H+1 根收盘判，H+2 开盘成交（`strategy_liquidation_reversal.py:351-352`，用例 ① 也钉死为 `T0+5H`）。
   - 这沿用了日线的「D+1 首根收盘判」语义，偏保守，不违反「不得提前」。但同一条小时流上两模板相差一根，请统领确认这是有意为之。
3. **Low：清算 1h 的首个判定小时没有收益，还多要一小时历史。**
   - `judged_periods` 的 `first = start 所在小时`（`strategy_liquidation_reversal.py:192`）。该小时没有前一根 K 线，必然是 `no_prior_day_candle`，却要求再往前多取一小时的数据。
   - 平台 precheck 的起点是否一致，我无法核对，因为 cutie-server 不在本树。若平台起点是 start+1h，会出现平台放行、provider 在最早边界拒跑。
4. **Info：金样的「不变」证明依赖作者的操作。**
   - `fixtures/arm_c0_p_daily_4h.json` 是新增未跟踪文件，我无法确认它是在 main 上生成的。
   - 它只覆盖 percentile 模式，没有 fixed 模式，也没有止损路径。
   - `TOOL_SPECS` 的两段 description 对 4h/1d 目录文案也改了，不在金样内。
   - 建议统领在 main 的干净 worktree 上跑同一生成脚本，对比 sha。

**已核对、无问题**
- **1h 取正确族/interval：** top 的 `need_range` 和 bind 在 `_bind_top_lsr`（`:11918-11929`）统一走 `1h`，仍是 ACCOUNT 的 `top_long_short_ratio`/Binance。清算走 `config.series_interval` 和 `series_step`（`:12004-12006`）。其余 timeframe 都落回 `1d`。
- **H+1 可用：** `available_at = ts + 3600`（`:1815`，由 interval 推出）。top 用 `available_at <= close`，清算用 `day + series_step`。两处都不会提前。
- **720 根回看：** 窗口是 `range(720,0,-1)`，H 本身不在内。fixed 模式 `lookback_bars` 为 0，`required_range` 用 `max(1,…)` 多取 1 根，与口径一致。
- **fixed 前一根：** 清算 fixed 的 `lookback_bars` 为 0 但用 `max(1,…)` 多取 1 根；top 无此需求。
- **缺根/空/重复拒跑：**
  - 缺根/空/重复：`_fetch_metric_series` 的空序列、重复时间戳、`_assert_contiguous` 都拦住。
  - 值为空：`canonical_decimal_str` 拒绝 `None` 和 NaN。
  - 首尾覆盖：top 的 `rows[0]/rows[-1]` 校验和清算 bind 的逐根校验补上首尾。
  - 没有补 0 或回退 1d 的分支。清算 bind 里的 1h 重复检查是冗余的，无害。
- **闭区间分块：** 90 天一块、`end_at=chunk_end-1`、`cursor=chunk_end`，边界根不丢。1h 每块约 2160 根，低于 5000 上限。
- **4h/1d 不变：** 4h/1d 分支逐项核对：`judged_periods` 回落 `judged_days`；`required_range` 回落 `lookback`；`lookback_bars` 等于 `lookback`；`timedelta(seconds=86400)` 等价 `days=1`；label 仍是 `utc_date`；`_common_assumptions` 只在 1h 追加覆盖。
- **风险路径：** 没碰 `_risk_*`、`_submit`、`_native_blocked_events` 的逻辑。top 的止损消费当前小时值的逻辑照旧。
- **性能：** top 是 O(N)。清算每小时一次 720 元素 Decimal 排序，约 100 天内是秒级，真正的瓶颈是 Finding 1。

**未动态执行的限制**
- 我没跑任何测试，也没做变异验证。
- 6 条用例「变异后会红」只是按代码路径推断：①②③④⑤ 的断言能抓到对应变异；③④⑤ 抓的主要是既有 `_fetch_metric_series` 路径。
- 没有对比 main 的金样字节。
- 没有读平台侧 precheck。

## 实施者核实

1. Medium 属实：5101小时完整HTTP清算响应触发现有 raw_report 1MB cap。分块用例直接验证生产 bind/fetch，共完整5820根（720回看+5100事件小时），首尾不丢；其它HTTP用例验证短窗交易和拒跑。未自行删减签名事件证据；长窗报告方案待 impl/统领裁定，PR先draft。
2. Low 判定时点：保留清算原「下一周期首根收盘判」时点，小时H+1收盘、H+2开盘成交；top 为H收盘（H+1边界）、H+1开盘成交。两者均满足H+1才可知，未擅自改变原清算决策时点。
3. Low 最早边界：已直接读 TokenBeep origin/dev 的 `_assert_hourly_liquidation_ready`，平台 first_required = floor(start/hour) - max(1,lookback_days*24) 小时；last_required = floor((end-1)/hour)-1小时。provider完全同公式，因此不存在所述平台/provider边界错位；首个小时无前一根OHLC仍保留no_prior_day_candle跳过。
4. Info 来源：已从未修改的 main@0e8b0ce git archive 单独重生成4份结果，cmp=0，SHA256一致，证据 /tmp/arm-c0-p/golden-main-reverify.log。fixed/止损日线行为另由相关整文件回归覆盖；金样只冻结运行结果，目录说明按任务书更新。

证据只覆盖本地合成数据及静态阅读；生产数据事实为ops补充，未独立访问生产。
