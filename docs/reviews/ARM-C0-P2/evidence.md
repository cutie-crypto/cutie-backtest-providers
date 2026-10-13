# ARM-C0-P2-P 本地证据

任务与解阻原文：tasks/ARM-C0-P2-P.md。原 rebase 结果 1f888cd 已按 impl 指令丢弃；从远端 d70fb82 恢复后 merge origin/main=0da4fc7，合并提交 5e0a360，无冲突、无 force push。

## 已证明的范围

只访问本地源码和合成 HTTP，不访问生产、真 PostgreSQL、Web 浏览器、RN 设备，不更新 provider pin、不合 PR。变更仅 liquidation_reversal 的 1h raw_report 形状与通用容量失败的机读 reason；funding/OI/ETF/netflow 的证据策略不改。权威运行契约同步 README.md 的 Hourly liquidation report evidence。

压缩在 cutie_backtesting_provider.py 的 settle_event_window_exits 后执行；成交归属、未决状态 guard 完整执行。事件 counters 仍描述全量，数组描述 retained，版本明确避免把数组长度冒充总数。所有 unknown/new outcomes 都保留；仅三种常规 skipped 原因进入 UTC 24h 摘要。摘要/全轨迹/外部指标均 canonical_json.v1；哈希不宣称平台已核验，也不能还原遗漏的原记录。

## 实测（HTTP provider 合成代理；UTF-8 紧凑 JSON）

- 3606h：旧 raw_report=1352962B，新=68971B；保留 1 个完整事件、151 个摘要；schema/trades/metrics/equity_curve/data_manifest 逐字节相同。
- 4326h：旧 raw_report=1622962B，新=82261B；保留 1 个完整事件、181 个摘要；schema/trades/metrics/equity_curve/data_manifest 逐字节相同。

统计/摘要测试独立遍历原全量事件，核段首末、无缺无重、状态/原因守恒、数值 min/max 和段/全轨迹 hash。默认720h预热中仅改单行值，事件 hash 不变而外部输入 hash 改变。dense 6000 拒单记录全部保留后仍超1MB并 INVALID_PARAMS fail-closed；exact1MB通过，UTF-8超限失败，reason/field/limit_bytes完整。

## 定向与变异

- 新增单文件初跑 1 failed / 5 passed（测试自身重复 settlement 后已 entered 记录的归属检查先报错）；改为用单独无状态事件直接验证 guard，之后 6 passed / EXIT=0。
- 存量定向初跑 1 failed / 93 passed，唯一旧断言要求每小时逐条 events；按新契约改为完整事件数+摘要数守恒，清算整文件65 passed / EXIT=0。
- 首次 consumers 变异把顶层收益改为1，新旧调用受同一代码污染仍绿；已用实现前 merge@5e0a360 的 git archive 独立捕获48h核心结果作为 arm_c0_p2_hourly_core.json，随后该变异红、cp恢复后绿。这是新金样；所有既有模板 JSON 金样没有修改，也未重算其它模板。
- hourly_sparse_3606_4326_budget：指定变异 RED_EXIT=1，cp恢复（字节核对）RESTORE_EXIT=0。
- summary_conservation_hash：指定变异 RED_EXIT=1，cp恢复（字节核对）RESTORE_EXIT=0。
- retain_attempts_native_and_fill：指定变异 RED_EXIT=1，cp恢复（字节核对）RESTORE_EXIT=0。
- consumers_and_legacy：指定变异 RED_EXIT=1，cp恢复（字节核对）RESTORE_EXIT=0。
- limit_message_and_dense_fail_closed：指定变异 RED_EXIT=1，cp恢复（字节核对）RESTORE_EXIT=0。

完整日志：/tmp/arm-c0-p2p/{t.log,s4.log,regression.log,mutation-last-two.log,*.red.log,*.restore.log,mutations.json,measurements.json,full.log,agy.json,agy.err}。精确命令均从指定 worktree 的 backtesting-py 执行；快信号 python3 -m pytest tests/test_arm_c0_p2_hourly_evidence.py -q -p no:cacheprovider；收尾 python3 -m pytest tests -q -p no:cacheprovider。

## main 基线

origin/main=0da4fc7 的 backtesting-py 源码/测试树与403333f逐字相同（git diff 403333f origin/main -- . 无输出）。既有 docs/reviews/PSF-P2-review.md 112行记录该树全量10685 passed / 165 warnings / EXIT=0（本任务复用已存证据，不冒称本轮重跑 main）。本轮源码树收尾全量10707 passed / 165 warnings / 571.37s / EXIT=0；相对 main 已存基线新增失败0。完整日志 /tmp/arm-c0-p2p/full.log 与 full.exit。异模型审查仍待返回，不宣称验收完成。

## Consumer 证据分层

平台服务核验、成交重放、示范收益和两端主读数所依赖的 schema/trades/metrics/equity_curve/data_manifest 与 trade_exit_kinds/legacy_metrics 保持相同；独立48h原始核心金样与新结果完全相同，4h/1d的旧 arm_c0_p_daily_4h.json 整体一致。消费者静态链见探路 /tmp/arm-c0-p2/REPORT.md 与 EVIDENCE.md：平台验证/成交重放/示范生成器不消费清算逐小时数组，两端无该数组渲染。本轮没有执行平台、设备或UI验证；端侧超限文案由 ARM-C0-P2-S 处理。

## 全量收尾

实现提交11977a2已普通push，PR102保持draft；full.log 为上述实现及当前独立核心金样的最终全量一次。后续若只补审查/证据文档不重复跑；任何实质返修须重新验证受影响范围。compile 与 git diff --check EXIT=0。
