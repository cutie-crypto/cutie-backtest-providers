# 9-T1：看涨吞没与锤子线 provider 交付证据

基线 `6f3dd6d`（origin/feat/1009-cx2-9p-swing）；工作树 `cx2-p1a`。
交付分支 `feat/1009-cx2-9t1-engulf-pin`，不改 main，不开 PR。
实现只在两个新模板注册、共享模板执行模块、时间层兼容用例、金样和说明内；9-P 纯函数源码经过变异后完整还原，没有新增最终 diff。

## 工具与参数

| 项 | 值 |
|---|---|
| 工具 | `local.backtesting_py.bullish_engulfing`、`local.backtesting_py.hammer_pin_bar` |
| direction | string，默认/唯一 `long`；short/both 在取数前 INVALID_PARAMS |
| position_filter | boolean，默认 true，可关闭 |
| reward_r | number，默认 2，范围 0.1–20 |
| exchange | 沿用环境默认，未覆盖时 okx |
| 共用键 | 仓位百分比/固定名义额、leverage=1、risk/time 关闭态默认与时间门禁/持仓到期 |

## codex-2 定

1. 两个 id 如上；R 参数叫 `reward_r`，避免与共用风控的 `take_profit_r` 冲突。只做多，现货/期货均沿用当前公开路径杠杆 1 门禁。
2. 形态根收盘确认，下一根开盘市价入场；止损/止盈以 High/Low 触发，下一根开盘市价出场。assumptions.pattern_execution 披露不会保证按触发价成交；不新增按价位成交层。
3. 冻结止损 = 两根吞没整体最低点或锤子线下影尖端 × 0.999；目标 = 实际引擎入场价 + (入场价−冻结止损)×R。开盘 ≤ 冻结止损，在该根订单处理前取消，不建仓、不改止损、不立即平仓。
4. 每次跳空取消在 raw_report.candle_pattern.skipped_entries 留原因、信号/入场根索引、开盘/冻结止损，并累加 skipped_entry_count。新信息不进 result.v2 的 trades/metrics/manifest 键集。
5. 固定 stop_loss_pct/take_profit_pct 键一旦给出即拒；ATR/R/trailing/breakeven/分档出场非零键与模板出场互斥。关闭态零值默认兼容；risk_layer_enabled 单独开启、仓位与持仓到期允许。同根优先止损 → 到期 → 止盈，不重复排平仓订单。
6. 位置过滤沿用 9-P：极值窗口排除当前根；吞没用两根整体最低点；锤子线新低严格比较。布林 20/2 用总体标准差；EMA20/60 用 adjust=False，满各自周期后可参与。触及是线值落在确认根 Low–High。指标只用截至当前收盘。
7. 开过滤 min_bars=21，关过滤=2；不足历史的 EMA60 分支不能通过，已就绪的新低/EMA20 分支仍可用。预热前缀与主区间合算，只取对应当前槽；末根无下一开盘禁止排新入场。
8. 两个 builder 均挂 @_with_time_config、继承现有风控/时间 mixin。跳空拦截只挂本次新模板的 broker 实例，仍调用原订单处理器，不影响旧模板。

## 手算与关态证据

- 吞没前根实体2、本根实体5，5≥2×1.2；差例实体2.399<2.4，虽包住前根也不成立。锤子线下影5、实体1、上影1、全长7；差例上影1.06>7.06×0.15=1.059。
- 原始尖端97 → 止损96.903；实际入场106 → 风险距离9.097；2R=124.194、3R=133.291。围绕各目标/止损两侧测试触发，实际出场独立取下一根开盘。
- 78 条新增定向用例涵盖几何、位置开关/极值与替代指标、止损/2R/3R、跳空等价及两侧、前缀/预热/未来后缀、同根优先、持仓到期、末根、取数前拒绝、HTTP 报告/冻结键集、时间门禁和关态字节。
- 新工具此前不存在历史交易基线。`fixtures/capture_9t1_candle_off.py` 手动绕过时间装饰器、不给 risk/time 键，分别采集预热开/关；每组真实成交1笔。`fixtures/9t1_candle_off.json` 冻结输入、trades/权益/result.v2 canonical 及 assumptions/raw_report 指纹。测试只读金样，不重生成；运行时枚举额外增加38例（时间兼容8、持仓到期2、风控特性8、杠杆20）。旧模板金样不动。

## 验证命令与结果

均使用 `python3 -m pytest ... -q -p no:cacheprovider > /tmp/9t1/<名>.log 2>&1` 后 `echo "EXIT=$?"`，无测试输出管道。

| 门 | cwd / 路径参数 | 结果 |
|---|---|---|
| 基线全量 | backtesting-py / 无 | 3710 passed，EXIT=0，baseline.log |
| 初次定向与时间层兼容 | backtesting-py / tests/test_9t1_engulf_pin.py tests/test_time_layer_compatibility.py | 616 passed，EXIT=0，focused.log |
| 收尾全量 | backtesting-py / 无 | 3826 passed = 3710基线 + 78新增 + 38枚举新增，EXIT=0，final.log |
| 全量失败后的定向复核 | backtesting-py / 新模板、时间兼容、风控3b、时间6b、杠杆 | 1364 passed，EXIT=0，focused-after-full.log |
| validator | validator / 无 | 29 passed，EXIT=0，validator.log |

本地测试最初发现若干期望写错（过滤关后的后续止损、极小风险导致入场根达到2R、引擎从第二根开始 next、现有 metrics 三键名称），修正手算期望后通过；不是产品修复证据。早期错 cwd 的命令 EXIT=4 不充当成功验证。
首轮全量 full-initial.log 为10 failed、3816 passed、EXIT=1：旧运行时枚举把所有单仓模板当成支持外置风控出场、并使用不产生新形态入场的通用行情。本批给互斥模式补明确 INVALID_PARAMS 断言、持仓到期换成手算形态行情；没有跳过枚举或删除既有用例。

## 七条可编译变异及还原

`/tmp/9t1/mutations.py` 每项复制备份→精确变异→py_compile→78条测试→复制还原→py_compile→78条测试→系统 cmp；不用 git checkout，还原强制重编译防同时间戳缓存。

| 变异 | 编译 / 改错测试 / 还原编译 / 还原测试 / cmp |
|---|---|
| 入场用形态根收盘价 | 0 / 1 / 0 / 0 / 0 |
| 止损不加0.1% | 0 / 1 / 0 / 0 / 0 |
| R写死2 | 0 / 1 / 0 / 0 / 0 |
| 跳空仍入场 | 0 / 1 / 0 / 0 / 0 |
| 位置过滤极值窗口读未来后缀 | 0 / 1 / 0 / 0 / 0 |
| 漏挂吞没时间层装饰器 | 0 / 1 / 0 / 0 / 0 |
| 吞没止损遗漏前根低点 | 0 / 1 / 0 / 0 / 0 |

每次还原78条全绿，脚本 EXIT=0；原始记录 mutations.log / mutations.json，各变异及还原日志分别保存，最终哈希 restoration.sha256。
这些仅为本地 provider 实现、HTTP 假取数和回归证据；不代表服务端注册、舰队 pin、生产部署或真实行情端到端验收。
