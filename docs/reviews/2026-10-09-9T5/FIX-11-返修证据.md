# FIX-11 斐波那契爆仓仲裁返修

基线02a9abc；codex-2定：显式价格键与intrinsic都将冻结initial_stop传入逐仓仲裁。
未设用户止损时initial_stop=None，仅处理爆仓；风险层关闭的止损保持收盘判断、下一开盘成交。
手写预期：信号Close108，3%止损104.76，20%止损86.4；实际入场109、L10，爆仓价98.1。
成交根Low95、Close96；下一根Open固定104.76。近止损reason=stop_loss、exit_price=104.76；远止损及未设止损均reason=liquidation、exit_price=98.1。

所有验证日志位于/tmp/fix11；pytest使用python3 -m pytest及-q -p no:cacheprovider，无管道，日志重定向后echo真实EXIT。
- 9t5-baseline-collect.log：5208 tests collected，EXIT=0（原头计数；不是重新跑原头全量）。
- 9t5-targeted-final.log：176 passed，EXIT=0（模板及通用爆仓仲裁）。
- 9t5-full.log：5211 passed，EXIT=0；N=5208+3=5211。
- 9t5-validator-tests-final.log：29 passed，EXIT=0；显式PYTHONPATH=validator使用当前源码。
- 9t5-validator-http.log：9T5 VALIDATOR PASS，EXIT=0；真实catalog/auth/schema/ASGI HTTP/引擎，OHLCV与plot为本地代理，1笔成交。
- 9t5-mutation.log：传参改回None后近止损变红，1 failed/2 passed，EXIT=1。
- cp备份/还原EXIT=0；9t5-cmp.log的cmp EXIT=0，随后完整全量绿。
- 初版测试用了错误raw_report键exits，3条失败（9t5-before/9t5-targeted，EXIT=1）；更正为既有exit_decisions后再变异和收尾，无产品语义调整。
- 初次validator单测误读已安装旧包，4 failed/25 passed（EXIT=1）；显式PYTHONPATH修正当前源码后29 passed。

只推feat/1009-cx4-9t5-fibonacci，不开PR、不合main；外部行情/Pre/生产未验证。
