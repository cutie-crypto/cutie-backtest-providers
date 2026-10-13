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

本地定向 35 passed / EXIT 0；确认队列删除 advance 变异两条红，cp 恢复两条绿。基线全量 10640 passed / EXIT 0；当前全量 290 failed, 10385 passed / EXIT 1；海龟相关失败 0。23 金样文件通过既有 capture 函数 / Q42C_RECORD 重算，入场与笔数不变；仍红的其它捕获和硬编码断言未完成，不能转 ready。

Sonnet 5.5 静态增量 review 未发现新成交逻辑缺陷，指出说明字段误报已修；覆盖缺口仍待做。钱路径统领逐行亲读后合，不合 main、不 pin。

详细增量及逐笔海龟底稿见 TokenBeep docs/reviews/2026-10-13-两线制/PSF-P2-review.md。历史 327aae0 的八族候选报告已由本记录替代。


## PSF-P2b-2 续清（2026-10-13）

origin/main 1e0f285，无冲突rebase；基线10645 passed/EXIT0，起始290失败节点无增减。本次只修改测试/捕获，生产代码、海龟与bottom guard不变。原290节点289 passed、1待裁红；完整全量 2 failed, 10678 passed, 164 warnings in 311.01s (0:05:11) / EXIT1，PR #96继续draft，不合main、不pin。

旧target-gap断言保留：entry121/target120，旧exit125/PnL330.576，候选exit121/PnL0；不在本批固化它。另有健康接口100ms门全量测得105.391ms：此前基线及rebase起始全量均绿，基线/候选单文件复验结果见TokenBeep docs，不能据单文件绿抹掉全量红，未修或降阈值。

金样由既有capture/canonical/digest重算，核基线与原文件相等、入场/笔数/方向及首笔Size不变；409次可比交易运行无入场变化，未执行调用不计。审查及审计在TokenBeep impl/psf-p2-docs 的 docs/reviews/2026-10-13-两线制/PSF-P2b-2-verification.md、PSF-P2-review.md末段。公共优先级披露同形待另单核清。
