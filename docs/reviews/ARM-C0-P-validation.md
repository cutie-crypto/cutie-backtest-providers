# ARM-C0-P 本地验证

基线 `origin/main=0e8b0cec6c55415a447acc9bb5eda1c6b983424c`；独立 worktree `../cutie-backtest-providers-arm-c0-p`，分支 `impl/arm-c0-p`。未触碰主 checkout，未连接生产。

- 编译：三个改动生产文件 `python3 -m py_compile`，EXIT=0。
- 六场景参数化 16 条；两 strategy 相关整文件加六场景共 127 条通过，EXIT=0，日志 `/tmp/arm-c0-p/related.log`。
- main 基线全量 10683 passed，EXIT=0，无基线失败 nodeid；日志 `/tmp/arm-c0-p/baseline-full.log`。
- 4h/1d 金样来自修改前 main，固定取证时间，覆盖交易、指标、assumptions、raw_report 四份结果；序列化字节完全一致。其它既有金样未改。
- 请求真实生产标签使用基础币 BTC；合成小时首根前缺根拒跑，既不写死 ops 观测日期，也不回退日线。
- 清算 hourly percentile 的首根为 start_hour - 720h（默认30天），fixed 为 start_hour - 1h。先前日报语义不变。
- 分块用例直接走两模板生产 bind（含真实 `_fetch_metric_series` 与每页5000条模拟上限），跨5101小时验证首尾及全部根数；不以大型报告序列化冒充取数验收。HTTP 成交/可用时刻及拒跑由其它定向用例覆盖。

## 六处变异与恢复

availability: mutation_EXIT=1 restore_EXIT=0 test=hourly_metric_available_at_H_plus_1
family_interval: mutation_EXIT=1 restore_EXIT=0 test=hourly_bind_uses_exact_family_interval
gap_zero_fill: mutation_EXIT=1 restore_EXIT=0 test=hourly_metric_gap_rejects
dedup_null_fill: mutation_EXIT=1 restore_EXIT=0 test=hourly_metric_duplicate_or_null_rejects
open_boundary: mutation_EXIT=1 restore_EXIT=0 test=chunked_fetch_keeps_boundary_bar
daily_changed: mutation_EXIT=1 restore_EXIT=0 test=daily_and_4h_unchanged

每处仅临时改动指定判据，单独定向执行，失败必须 EXIT=1；备份字节恢复后相同用例必须 EXIT=0。最终源码恢复一致。

全量最终结果与异模型审查在收口时补充。证据仅为本地合成数据，不证明已部署环境行为。
