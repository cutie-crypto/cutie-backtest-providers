# WARMUP-B-4 本地验证

基线5d27d91；工作树/Users/tf/Documents/bitbucket/cutie-backtest-providers-warmup-b，impl/warmup-b从2840fa9普通快进至5d27d91，干净后开工。不碰main、不pin、不部署。

蜡烛 frozen样本8/16/4 passed；直接HTTP旧/新16响应金融字段变化0；两个单文件78/215 passed。plow三个单用例样本各1 passed；整文件110 passed/15 failed，新增3个金融停止请求chan_3buy/chan_3sell/ema_rsi_pullback保留原短前缀、不更新金样。金融字段trades/equity_curve/metrics/data_manifest使用旧版678b4e1指纹，完整请求+主行情manifest摘要匹配；58条plow、13条蜡烛去重记录。非预热诊断不重录。

金融守卫变异：将adx_di_cross total_return替换12345，原缺口行为断言无法识别但金融hash拒绝，pytest EXIT=1；cp还原字节一致，再跑1passed EXIT=0。compile/diff EXIT=0。

51原金样字节变化0；B3的30金融/42诊断、patconf2b1六组/P6十响应及22/187/177原簇不改文件。Sonnet5.5一轮只读及闭环已追加原review文件。两目标簇仅部分清理，不以DONE表示全量通过。

唯一收尾全量待完成；完整证据保存在/tmp/warmup-b4。任务书原文单独落TokenBeep分支impl/warmup-b4-doc @be62f52，普通push EXIT=0，未合dev。
