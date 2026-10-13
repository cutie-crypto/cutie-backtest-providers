# WARMUP-B-3 夹具适配与停止证据

任务范围为B-2的649条完整响应/指标金样簇、728条旧HTTP成功期望簇，任务书在同目录WARMUP-B-3-task.md。当前为本轮部分清理已收口，PR98继续draft；不合main、不pin。

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

收尾唯一全量与逐nodeid对照见下节。未清零前不将DONE哨兵解释为全量验收通过。

## 收尾唯一全量（源码检查点1e3d49f）

- 仓根命令：python3 -m pytest -q backtesting-py/tests --junitxml=/tmp/warmup-b3/full.xml > /tmp/warmup-b3/full.log 2>&1; echo "EXIT=$?"。实际保存原pytest退出值于full.exit。
- 结果：9010 passed / 1659 failed / 0 errors，EXIT=1；相对B-2的8682/1987，通过增量+328，失败增量-328。
- 按nodeid：解决B-2失败328条，新增失败/错误0条。完整归因在/tmp/warmup-b3/full-failure-attribution.tsv，新增列表/簇计数在full-comparison.json。
- 返修后三个受影响文件合计1466 passed/291 failed，新增失败nodeid=0；分文件entry=778/115、mtf=109/121、time=579/55，EXIT=1，未声称全绿。
- 金融守卫与非预热报告守卫变异均EXIT=1，overlay均未覆写，cp/字节还原一致；返修正向捕获EXIT=0。全量前后所有.py/.json源码冻结hash变化0；收尾提交仅补docs。
- 51个B-2金样逐字节变化0（另有warmup demo同样原样），原time/entry JSON未编辑，P6/patconf2b1与生产provider原样。
- 当前是两簇的部分清理；原金融变化30组及非披露报告变化42组停止。原始22/188/177簇与patconf2b1六组、P6十响应待裁继续排除，不改判据/金样。
- 高周期完整冻结复放、1M非固定网格继续为本单外遗留。无main合入、pin、部署、Pre或真实行情验收。
- 全量仍红则PR98继续draft；DONE仅为本轮收口哨兵，不代表整批验收通过。

剩余簇（沿用B-2归因，不擅自改判）：
- 过滤/高周期完整前缀不足或旧闭合断言：22
- 新严格拒跑与旧HTTP成功期望冲突（待核）：614
- 旧预算/起止/缺口判据未适配（待核）：177
- 其它旧fixture或指标断言（待逐例核实）：187
- 完整响应/指标金样未适配（待核）：437
- 入场变化停止件，保留未适配桩：222

本轮解决簇：
- 完整响应/指标金样未适配（待核）：212
- 新严格拒跑与旧HTTP成功期望冲突（待核）：114
- 入场变化停止件，保留未适配桩：1
- 其它旧fixture或指标断言（待逐例核实）：1

共享目标夹具的间接影响（未重分类、未改这些测试文件/断言）：
- backtesting-py/tests/test_p6_trade_exit_kinds.py::test_every_v2_tool_reports_one_kind_per_trade[red_streak_rsi]（B-2归在入场变化停止件，保留未适配桩）随共享成功夹具转绿。
- backtesting-py/tests/test_pin7e_final_close.py::test_final_rerun_unclosed_trade_degrades_to_engine_error（B-2归在其它旧fixture或指标断言（待逐例核实））随共享成功夹具转绿。

在自身worktree detached于2840fa9实施，目标分支被另一个worktree占用；按HEAD普通推送impl/warmup-b，不移动另一worktree的本地分支，不碰主checkout。
