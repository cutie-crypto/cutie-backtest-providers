# PSF-P2：七族原生保护成交候选，可审未合 main

当前收口：PSF-P2b-3基线与候选全量均绿，引入失败0；PR #96可审，未合main、未pin。以下各批记录按时间保留，最新门结果见末段。

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

## PSF-P2b-3（2026-10-13，已裁底部止盈跳空机理）

按统领裁定，硬编码 bottom 用例锁定入场根54/入场121/一笔多头，出场根54/价121/PnL0；冻结TP120已被入场开盘越过，原生保护单当根按开盘121成交。fixtures/README.md 同步机理，未改JSON或生产bottom guard，PSF-BOTTOM-GAP仍另单。

普通rebase会重放此前ours衔接的两份历史并触发3处冲突；已abort并用rebase --rebase-merges保留合并结构，成功到origin/main678b4e1。原a440830历史通过ours merge衔接，代码树不变，只普通push；未合main、未pin。

Claude Sonnet5.5增量只读审查（实际modelUsage=claude-sonnet-5-5）1条低优先建议：补锁long方向，已补。审查输入中称整文件绿的描述不准确：首次根目录快信号4红/136绿，均为Q42C用例名未匹配；审查未验证运行门。修正为backtesting-py cwd后复验，详见/tmp/psf-p2b3/focused-correct.log。首次根目录全量48红均属于Q42C cells，弃作无效门，保留full-root-invalid.log/xml；有效门与同命令基线重新从backtesting-py运行pytest tests，不改代码绕过守卫。

WARMUP-B-2按改派停止，源返修未提交留在其独立worktree；handoff /tmp/warmup-b2/HANDOFF.md，已开始的候选全量SIGTERM/EXIT143，无完成门。交codex-3续做，当前线不再接。

有效收口门（相同命令 python3 -m pytest -q tests，各自在 backtesting-py cwd）：origin/main678b4e1 基线10646 passed/185 warnings/291.27s/EXIT0；候选10681 passed/164 warnings/293.87s/EXIT0，引入失败0。q17b 单文件140 passed/EXIT0。无运行源码变更；JSON未改；新断言锁定54→54、121→121、PnL0、long且一笔。机理 README 与统领裁定一致。PR #96满足ready条件，合并与10-15 pin仍另需放行。

过程偏离：误cwd的48红全量证据弃用后纠正，未改守卫或金样；此前已推的两条提交标题误写成Unicode字面转义，遵守禁force/不改已推历史，后续通过UTF8消息文件提交正常中文。
