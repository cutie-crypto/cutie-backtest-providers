# WARMUP-B-3 夹具适配与停止证据

任务范围为B-2的649条完整响应/指标金样簇、728条旧HTTP成功期望簇，任务书在同目录WARMUP-B-3-task.md。当前为已完成部分检查点，PR98继续draft；不合main、不pin。

两簇各三样本先复现（/tmp/warmup-b3/*sample-*-before.log）。共同根因是空或固定60根历史的旧桩不满足新R，生产provider不改。51份B-2改过的金样与额外warmup demo逐字节不变。P6与patconf2b1原文件不改。

- 时间/过滤HTTP候选223组：145组交易、净值、指标、主manifest全字段等于main；30组金融字段变化停件；48组没有可用的通用成功夹具。review发现145中42组信号诊断/legacy_metrics有变化，进一步停止，最终103组允许完整声明历史。
- 完整播种为目标测试显式complete=True；共享capture默认保持旧行为，仅注册且等于default的time/filter参数能剥离。time.BASELINE使用deepcopy避免修改其他测试持有的原JSON对象。
- 现成snapshot/response/ledger_fingerprint捕获元数据overlay（70过滤、40single、3ledger）；完整金融字段和非预热报告投影都有冻结SHA守卫，保留原engine/result.v2/min_bars约束。single写入audit，捕获时验证原min_bars。
- 两份red_streak金样通过现成canonical捕获；30组仓位响应金融字段变化0。10b2d单文件31 passed、EXIT=0。
- pin7e只适配flat-channel run；P6 helper原样。101成功响应金融字段0变化；初轮单文件87 passed/109 failed，保留泛化冷路径。
- shortpat1仅适配直接http，26成功响应金融字段0变化，44失败响应的error_type/error_message/limitations原样；初轮131 passed/12 failed，共享capture冷路径仍红。
- 原样样本文件isolated 28 passed/121 failed、plow3 9 passed/116 failed，EXIT=1；不修改本单排除的连续性/高周期判据。
- Claude Sonnet 5.5增量只读一轮，实际modelUsage核对，已追加原review；作者处置与审查限制同页。不会把作者返修记成外部复审。
- 金融哈希变异：捕获脚本EXIT=1且overlay未写，还原原registry字节一致；原正向捕获EXIT=0。最终返修捕获EXIT=0，compile/diff检查EXIT=0。

本机证据只证明合成测试数据下适配；cold seed复制首根主OHLCV，零交易格只证明未新增交易，不是生产行情、读取连续性或新版指标行为验收。

收尾唯一全量待执行；最终counts、B-2失败nodeid逐项对照、源码冻结校验追加于此。未清零前不将DONE哨兵解释为全量验收通过。
