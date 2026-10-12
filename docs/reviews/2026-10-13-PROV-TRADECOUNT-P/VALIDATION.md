# 最终验证

基线 origin/main@30d6a07；独立 feat/prov-tradecount；仅字段、目录、README、用例与证据改动。
- 编译级快信号：修改模块和新增测试 py_compile EXIT=0。
- 定向：python3 -m pytest tests/test_trade_count_outlets.py tests/test_portfolio_10d.py tests/test_portfolio_rotation_10e.py tests/test_btc_dominance_rotation_48.py -q；101 passed, 6 warnings in 19.10s，EXIT=0。
- 新增测试11条：6个v4出口；零成交、买入未卖出、仅部分减仓、跨lot卖出4条；全部成功出口覆盖1条（14个真实运行结果及两工具目录声明）。非v4已有实现未改。
- 变异：删组装器trade_count赋值；test_v4_outlet_closed_count_string[runner-2]，1 failed / KeyError: trade_count，EXIT=1；cp还原，cmp EXIT=0；10 passed EXIT=0。review新增第11条后最终定向101条通过。
- 全量：backtesting-py 下 python3 -m pytest tests -q；10651 passed, 187 warnings in 285.52s (0:04:45)，EXIT=0。
- 全量后编译：py_compile全部顶层模块及tests递归Python文件，py_compile OK: 236 files，EXIT=0。
- git diff --check EXIT=0；provider未发现tests/governance或scripts/governance/check-rules.sh，本单未改/未执行TokenBeep治理门。
- 手写portfolio_10d_golden.json只添metrics.trade_count="2"；删该字段与origin/main JSON结构相等，EXIT=0，无其他哈希/字节金样变化。
- Claude静态review PASS；低级测试建议已修，无阻断/高/中。原文CLAUDE-REVIEW.md，处理REVIEW-RESOLUTION.md。
- 消费者仅源码/键集证据，详见CONSUMER.md；无Pre、生产或pin验收。

日志在 /tmp/prov-tradecount-p/{final-target,full,compile,mutation-red,mutation-restored}.log。前期失败/被中断日志不作绿证据。
