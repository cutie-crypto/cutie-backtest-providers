# 改前出口清单（origin/main @30d6a07）
| 出口 | 文件:行 | 当前类型 / 处理 |
|---|---|---|
| 单标的 v2 组装 | backtesting-py/cutie_backtesting_provider.py:9646 | int len(trades_v2)，不动 |
| scale_in_out（RSI/grid/DCA） | backtesting-py/cutie_backtesting_provider.py:9887 | int len(trades_v2)，不动 |
| 单标的 / scale HTTP | backtesting-py/cutie_backtesting_provider.py:11486 / 9957 | 透传上述 metrics，不动 |
| 单标的 StrategySpec replay | backtesting-py/strategy_kernel.py:4328 | int len(state.trades)，不动 |
| basket v3 simulation / 组装 | backtesting-py/strategy_kernel.py:4864；strategy_execution.py:1630 | int len(trades) / 透传，不动 |
| basket HTTP | backtesting-py/cutie_backtesting_provider.py:10456 | 透传 v3，不动 |
| Freqtrade parse / HTTP | freqtrade/cutie_freqtrade_provider.py:716 / 1437 | int total_trades / 透传，不动 |
| v4 组装 | backtesting-py/portfolio_result_v4.py:165 | 缺字段；补卖出成交数的非负整数字符串 |
| portfolio runner | backtesting-py/portfolio_runner.py:69 | 继承共用组装 |
| 周轮动 / BTC.D轮动 | backtesting-py/portfolio_rotation.py:236；portfolio_btc_dominance_rotation.py:136 | 继承 runner |
| 两轮动 HTTP / 目录 | backtesting-py/portfolio_rotation_http.py:143 / 53 | HTTP透传；目录补字段声明 |

v4 ledger 没有独立 trades 数组；闭仓笔数按 fills 中 side=sell 的成交记录计，一次部分减仓或跨多个 FIFO lot 的卖出各计一笔。未卖出的持仓和买入不计。
