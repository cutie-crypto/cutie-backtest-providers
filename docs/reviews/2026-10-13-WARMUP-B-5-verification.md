# WARMUP-B-5 本地验证

impl/warmup-b沿e2aea60 merge origin/main@0da4fc7，不rebase；合并f933b65。16个金样冲突保留原字节；51份冻结金样相对e2aea60变化0，上游金融/报告变化列表见artifacts/WARMUP-B-5/merge-golden-stops.json。本轮夹具增量相对f933b65，生产代码与断言不改，原B3/B4停止件及待裁文件不动。

完整请求+主数据hash匹配才补合成历史前缀，旧金融hash核对trades/equity_curve/metrics/data_manifest。捕获使用678b4e1 provider源码、0da4fc7策略模块，不能表示历史部署响应或真实行情。44个审计节点金融不变，实际适配两文件42个原失败；ema_rsi_pullback金融变化保持失败，缠论本轮不改。既有金融金样不重录。

审计复现：在f933b65干净导出中复制本批audit-targets.py后，从仓根运行python3 audit-targets.py old/new；old读取git对象678b4e1，new只拦历史mock，接受新关键字但旧mock只传位置参数。输出/tmp/warmup-b5/targets-old/new.json。逐节点同请求比较四金融字段，变化节点禁止纳入新表，结果见financial-audit.json。完整响应和日志在/tmp/warmup-b5，仅本地合成行情。

三样本3passed EXIT0；两文件239passed/5failed EXIT1（停止件及原test_none路径）。收益注入12345变异1failed EXIT1；cp还原字节一致，1passed EXIT0。编译/diff EXIT0。

Owner 1013新令覆盖审查模型：未开始的Claude审查改opencode/agy，本轮opencode DeepSeek 4.1 Flash只读结果追加原review。

全量命令以任务书明示为准：cd backtesting-py && python3 -m pytest tests -q -p no:cacheprovider。pin直接基线/tmp/psf-p2c/full.log=10683passed/0failed（同cwd/命令，845.69s）；B4@238ed39同命令9106/1563，后续e2aea60恢复6个排除节点，因此B4结果非e2aea60同版全量。最终全量及两口径风控遗留收尾追加。
