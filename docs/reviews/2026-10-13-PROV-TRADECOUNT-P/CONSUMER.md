# TokenBeep 消费者只读核对

核对最新 origin/dev @e456ffe06f90e3d599923264d6fe06471004946b；git show 提取源码，不切换、不修改 TokenBeep。
- cutie-server/services/portfolio_result_v4.py:88-91 接受旧三键或另带 trade_count；值必须是非负整数字符串，旧指标按原方式重算。
- cutie-connector/packages/connector/src/portfolio-result-v4.ts:71 导出 validateResultV4；:165 精确只认 total_return/max_drawdown/fill_count，新键会返回 metrics decimal keys invalid。
- cutie-connector/packages/connector/src/backtest.ts:1 再导出该函数，没有调用。
- 实际调用仅 cutie-connector/packages/connector/src/portfolio-result-v4.test.ts:34,40,58,61,64,67；:3 导入。
- 全 cutie-connector 搜索 validateResultV4 无其他调用；packages/connector/src/index.ts 未导出它。仓外消费者不在此证明范围。
- 提取精确键集谓词证明旧三键相等、新四键不等，EXIT=0；这是静态源码证据，没有执行完整 validator 或 Pre API。
- 目录另行核对：backtest.ts:1824-1830 检查 output_schema 为对象及 metrics 为字符串数组，未固定三项；server services/backtest_provider_catalog.py:431 保留 output_schema 结构，不固定 metrics 三键。
- 下次 pin 前由统领判断消费者兼容跟进；本单只读，不改 TokenBeep。
