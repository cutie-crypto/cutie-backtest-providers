# WARMUP-B-5 本地验证

impl/warmup-b沿e2aea60 merge origin/main@0da4fc7，不rebase；合并f933b65。16个金样冲突保留原字节；51份冻结金样相对e2aea60变化0，上游金融/报告变化列表见artifacts/WARMUP-B-5/merge-golden-stops.json。本轮夹具增量相对f933b65，生产代码与断言不改，原B3/B4停止件及待裁文件不动。

完整请求+主数据hash匹配才补合成历史前缀，旧金融hash核对trades/equity_curve/metrics/data_manifest。捕获使用678b4e1 provider源码、0da4fc7策略模块，不能表示历史部署响应或真实行情。44个审计节点金融不变，实际适配两文件42个原失败；ema_rsi_pullback金融变化保持失败，缠论本轮不改。既有金融金样不重录。

审计复现：在f933b65干净导出中复制本批audit-targets.py后，从仓根运行python3 audit-targets.py old/new；old读取git对象678b4e1，new只拦历史mock，接受新关键字但旧mock只传位置参数。输出/tmp/warmup-b5/targets-old/new.json。逐节点同请求比较四金融字段，变化节点禁止纳入新表，结果见financial-audit.json。完整响应和日志在/tmp/warmup-b5，仅本地合成行情。

三样本3passed EXIT0；两文件239passed/5failed EXIT1（停止件及原test_none路径）。收益注入12345变异1failed EXIT1；cp还原字节一致，1passed EXIT0。编译/diff EXIT0。

Owner 1013新令覆盖审查模型：未开始的Claude审查改opencode/agy，本轮opencode DeepSeek 4.1 Flash只读结果追加原review。

全量命令以任务书明示为准：cd backtesting-py && python3 -m pytest tests -q -p no:cacheprovider。pin直接基线/tmp/psf-p2c/full.log=10683passed/0failed（同cwd/命令，845.69s）；B4@238ed39同命令9106/1563，后续e2aea60恢复6个排除节点，因此B4结果非e2aea60同版全量。最终全量及两口径风控遗留收尾追加。

## 最终完整全量与收口

直接全量源码373ee40，9074 passed / 1634 failed / 0 errors，EXIT1，545.17s。之后仅文档提交，backtesting-py字节差异0。首轮全量为审查返修中止，EXIT143，不计为完成证据；本次是唯一完整收尾全量。
相对同命令pin基线0da4fc7（10683/0）新增失败1634，完整nodeid列表非空；目录独有12条按统领裁定排除后有效新增1622，列表仍非空。两目标簇剩完整响应391、旧HTTP493，ready不成立，PR98继续draft，单独攒PIN19，无日期。
相对B4检查点238ed39：解决43个旧失败（本轮两文件直接恢复42；另1为上游差异），表面新增114；114节点在本轮夹具修改前的合并基线f933b65、同backtesting-py命令逐条复现114failed EXIT1，32.08s；未复现列表为空。此归因不能将相对pin的1634新增失败说成0。
51份原金样变化0；compile/diff EXIT0；金融守卫注入12345变异红、cp还原绿；审查失配闭环373ee40，主行情首条Open改变会立即拒绝。普通push，不改main、不pin。
风控遗留：backtesting-py/tests/_q42c_default_path.py:79，35选定节点仓根23failed/12passed EXIT1、指定子目录35failed EXIT1；仅目录独有12节点为0/12，登记TokenBeep BT-PROVIDER，不计本单有效失败。其它23条不排除。
原B3/B4金融变化30组/诊断变化42组、patconf2b1六组、P6十响应及原排除簇继续不适配。上游合并16个金样冲突、本轮ema_rsi_pullback和缠论金融变化停止件见两份审计清单。full-comparison、归因TSV、两种新增nodeid清单、风控目录节点、51金样和实际模型均持久化在本批artifacts目录；完整日志仍在/tmp/warmup-b5。
