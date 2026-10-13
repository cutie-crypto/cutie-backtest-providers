# PSF-P2b：七族候选，保持 draft

本批只覆盖七族；海龟完整恢复 30d6a07，海龟金样未动，新海龟用例与变异已移出。

| 原候选 518 失败逐条分类 | 数量 | 例子 |
|---|---:|---|
| 基线就红 | 0 | 无：30d6a07 全量 10640 passed / EXIT 0 |
| 本单引入：出场价 / 时间及派生金样断言 | 433 | test_r_target_from_actual_entry[True-2-bullish_engulfing]：旧 ExitBar=28，新=27 |
| 本单引入：其它 | 85 | test_omitted_sizing_keys_match_5d0f9ac_bytes[morning_star-leverage2]：_risk_exit_reason 未初始化导致 ENGINE_ERROR（已修）；还包含确认队列 / 优先级 / 旧源码字节锁 / 海龟入场变化 |

| 金样文件（相对 backtesting-py/tests；价 / 时间变动笔数） | 价 / 时间；入场、笔数均不变 |
|---|---|
| fixtures/7p3b2_off_c72c4a1.json；7p4_off_1d66844.json；patconf1_5ddd8eb.json；plow2a_b48e65e.json | 4/4；3/3；19/19；10/10 |
| fixtures/entry_filters_5b8320a.json | 40/40 |
| golden_10b2a/{macd_bullish_divergence.default.json,macd_bullish_divergence.compare_hist.json,rsi_bullish_divergence.default.json,rsi_bullish_divergence.leverage2.json} | 各 0/0（说明字段变化） |
| golden_10b2a/{fibonacci_retracement.default.json,fibonacci_retracement.user_stop3.json} | 1/1；0/0 |
| golden_10b2b/{macd_bearish_divergence.default.json,macd_bearish_divergence.compare_hist.json,rsi_bearish_divergence.default.json,rsi_bearish_divergence.leverage2.json} | 各 0/0（说明字段变化） |
| golden_10b2b/{chan_3sell.default.json,chan_3sell.leverage3.json,chan_3buy.default.json,chan_3buy.bi_old.json} | 1/1；1/1；1/1；0/0 |
| fixtures/{9t1_candle_off.json,9t2_candle_off.json,9t3_bottom_off.json} | 4/4；8/8；4/4 |
| fixtures/q42c_default_path_goldens.json | 0/0（75 个响应，73 个用例；说明字段变化） |

本地定向 35 passed / EXIT 0；确认队列删除 advance 变异两条红，cp 恢复两条绿。基线全量 10640 passed / EXIT 0；当前全量尚待回填。23 金样文件通过既有 capture 函数 / Q42C_RECORD 重算，入场与笔数不变；仍红的其它捕获和硬编码断言未完成，不能转 ready。

Sonnet 5.5 静态增量 review 未发现新成交逻辑缺陷，指出说明字段误报已修；覆盖缺口仍待做。钱路径统领逐行亲读后合，不合 main、不 pin。

详细增量及逐笔海龟底稿见 TokenBeep docs/reviews/2026-10-13-两线制/PSF-P2-review.md。历史 327aae0 的八族候选报告已由本记录替代。
