# WARMUP-B-4 本地验证

基线5d27d91；工作树/Users/tf/Documents/bitbucket/cutie-backtest-providers-warmup-b，impl/warmup-b从2840fa9普通快进至5d27d91，干净后开工。不碰main、不pin、不部署。

蜡烛 frozen样本8/16/4 passed；直接HTTP旧/新16响应金融字段变化0；两个单文件78/215 passed。plow三个单用例样本各1 passed；整文件110 passed/15 failed，新增3个金融停止请求chan_3buy/chan_3sell/ema_rsi_pullback保留原短前缀、不更新金样。金融字段trades/equity_curve/metrics/data_manifest使用旧版678b4e1指纹，完整请求+主行情manifest摘要匹配；58条plow、13条蜡烛去重记录。非预热诊断不重录。

金融守卫变异：将adx_di_cross total_return替换12345，原缺口行为断言无法识别但金融hash拒绝，pytest EXIT=1；cp还原字节一致，再跑1passed EXIT=0。compile/diff EXIT=0。

51原金样字节变化0；B3的30金融/42诊断、patconf2b1六组/P6十响应及22/187/177原簇不改文件。Sonnet5.5一轮只读及闭环已追加原review文件。两目标簇仅部分清理，不以DONE表示全量通过。

唯一收尾全量结果与目录差异说明见下节；完整证据保存在/tmp/warmup-b4。任务书原文单独落TokenBeep分支impl/warmup-b4-doc @be62f52，普通push EXIT=0，未合dev。

## 唯一全量、基线目录差异与收口范围

命令严格在backtesting-py内：python3 -m pytest tests -q -p no:cacheprovider > /tmp/warmup-b4/full.log 2>&1; echo EXIT=$?。
检查点238ed39：9106 passed / 1563 failed / 0 errors，EXIT=1；对B-3记录9010/1659为+96/-96。
原B-3从仓根运行，nodeid为backtesting-py/tests/...；本单从子目录运行，nodeid为tests/...，会激活原先未匹配的q42c_default_path.cells。35条表面新增均在原5d27d91、同backtesting-py目录、同-p no:cacheprovider逐节点复现：35 failed，EXIT=1；实际代码引入新失败0。逐节点差异与基线证明分别为full-comparison-reconciled.json和baseline-same-command-new35.log。未改该风控金样/插件，不冒充全量绿。
初轮解析把2条logging ERROR当用例，并截断带空格参数名；已按pytest摘要的tests/前缀与完整参数修正，1563个真实失败与结果行一致。

全量检查点的131条旧失败恢复中有6条原归在177排除簇的plow边界用例。收口已使这6条显式complete=False并恢复共享post默认的原lambda路径，断言原样；最终单文件104 passed /21 failed，EXIT=1，6条排除节点均保持旧失败。最终只恢复原两目标簇；余下22/187/177排除簇未适配。收口补丁10行增加/9行删除作者亲读，无产品代码/金融金样改动，不再跑第二次全量。因此9106/1563是238ed39的直接全量证据，不能表述为收口SHA的全量结果；最终SHA只有此单文件增量验证。

51原金样及任务书字节不变。新增3金融停止请求已登本文件；B3的patconf2b1六组/P6十响应及30金融/42诊断原状态保留。严格判据仍有目录口径差异及收口SHA缺全量证据，保持PR98 draft，待下一单裁定目录口径/收尾门，不合main、不pin。
