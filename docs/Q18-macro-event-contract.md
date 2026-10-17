# Q18：宏观事件触发模板（H1–H3）

基线：`39bc346`（生产 pin 3）。仅新增 provider 工具；不读事件表、不改服务端、不改生产 pin。
需求来源：TokenBeep `docs/research/2026-10-08-Jessie回测模板需求-53条常用策略.md` 第 35–37 条；H2 空值遵从统领第 24 封裁定。

## 工具与参数

所有工具的 `events` 必填，长度 1–50；对象和顶层参数均拒绝未知键。

| 工具 | 专有参数及默认值 |
| --- | --- |
| `local.backtesting_py.macro_release_breakout` | `pre_window_minutes=30`、`max_hold_minutes=240`、`take_profit_r=2`、`direction="both"`（long/short/both） |
| `local.backtesting_py.macro_surprise_direction` | `surprise_threshold=0.1`、`threshold_inclusive=true`、`direction_map={"below":"long","above":"short"}`、`hold_minutes=1440`、`stop_loss_pct=3` |
| `local.backtesting_py.fomc_reversal` | `lookback_hours=72`、`move_threshold_pct=5`、`threshold_inclusive=true`、`entry_delay_minutes=30`、`hold_minutes=2880`、`stop_loss_pct=3` |

分钟/小时参数必须为整数，通常为 1–1000000；`entry_delay_minutes` 可为 0。
`take_profit_r` 为 0.01–100；`surprise_threshold` 为 0–1000000；`move_threshold_pct` 为 0.01–1000000；`stop_loss_pct` 为 0.01–99。
数值必须有限，布尔值不作为数值接受。`direction_map` 必须恰有 below/above，两者各为 long/short/none。

三者支持既有单仓仓位参数（百分比、名义金额、按风险定仓及 compound/数量步长）、leverage 和 exchange。
模板自管止损、止盈、持有时钟，不发布通用指标过滤、ATR、动态止损、分批止盈、可选时间层参数；传入这些参数会拒绝，避免无效配置和预热依赖。
H1 仅 direction=long 时可用于 spot；H2 仅映射不含 short 时可用于 spot；H3 为 futures-only。

## 严格阈值参数（PROVIDER-STRICT-CMP-1018）

H2、H3 及 `local.backtesting_py.funding_settlement_reversal` 支持 `threshold_inclusive`，仅接受 JSON boolean，默认 true；null、0/1、字符串均拒绝。
三个工具分别比较 `abs(actual-expected)` 与 `surprise_threshold`、`abs(move_pct)` 与 `move_threshold_pct`、`abs(rate_pct)` 与 `rate_threshold_pct`。
true/省略时含等号；false 时等于阈值跳过，大于阈值才允许入场。资金费率默认仍为 `rate >= +threshold` 做空、`rate <= -threshold` 做多；严格模式分别为 `>`、`<`。
参数通过现有 schema 派生目录和白名单，非法类型在取数前拒绝；直接配置解析也拒绝非法类型。结果 assumptions 回显实际布尔值，H2 threshold 与 funding rate_rule 同步表达实际执行口径。
严格模式等值跳过沿用 `surprise_below_threshold` / `move_below_threshold` / `rate_below_threshold` 原因码；这些码表示未满足阈值判据，严格模式下包括相等。H1 不接此参数，原先突破必须严格越界的口径不变。
服务端识别用户严格句式并传 false 属于甲-2；本 provider PR 不含服务端改动、合并、发布或 pin。

## 下发参数契约

事件数据源为 `data_source=inline_params_only`。服务端从事件表筛选后内联下发，provider 不查询事件表或事件 API。
行情仍走既有 OHLCV 获取路径；事件数据源声明不替换行情来源声明。

- H1、H3：每项恰为 `{ts_utc, label}`。
- H2：每项恰为 `{ts_utc, label, expected, actual}`；`expected` 对应事件表 `expected_value`，`actual` 对应 `actual_value`。两列为可空 NUMERIC；请求中转换为 JSON number 或 null，不能传数字字符串。字段必须存在，null 不等同于缺字段。
- `ts_utc` 使用 EVENT0 的 ISO8601 格式，必须有 Z 或显式偏移；统一为 UTC 后不允许重复，输入顺序不影响按时刻处理。
- `label` 为 1–64 字符的非空白字符串。事件总数为 1–50。
- 服务端负责时间换算；H3 直接使用 `ts_utc`，引擎不再进行美东时间/DST 换算。
- H2 使用 `Decimal(str(actual)) - Decimal(str(expected))`，避免 2.9−2.8 的浮点误差把恰等于 0.1 的事件过滤掉。默认 `threshold_inclusive=true`（不传也为 true），差值绝对值等于阈值时可交易；传 false 时必须严格大于阈值。差值为 0 时没有方向，两种模式都不交易。

```json
{
  "events": [
    {"ts_utc": "2026-01-13T13:30:00Z", "label": "CPI", "expected": 2.9, "actual": 2.8},
    {"ts_utc": "2026-02-13T13:30:00Z", "label": "CPI unavailable", "expected": 2.9, "actual": null}
  ],
  "surprise_threshold": 0.1,
  "direction_map": {"below": "long", "above": "short"},
  "hold_minutes": 1440,
  "stop_loss_pct": 3
}
```

## 成交、精度及跳过规则

- 复用 EVENT0 解析、事件根定位（open ≤ ts < open+period）、零指标预热分支和基于最终 result.v2 的事件成交对账。`strategy_event_window.py` 本身不修改。
- H1：在公布前回看区间中仅取完整 K 线的最高/最低价；公布后的收盘严格高于/低于该区间时提交市价单，下一根开盘成交。止损为区间另一侧；止盈为实际成交价 ± `take_profit_r × abs(实际成交价−止损)`。下一开盘跳空穿过冻结止损时撤销入场，事件仍消费一次。
- H2：事件与开盘对齐时在该时刻开盘入场；不对齐时在其后的第一个开盘入场。仅使用该事件的内联公布/预期值决定方向，不读公布后 K 线价格决定方向。
- H3：累计涨跌为 `(回看区间最后完整 K 线收盘 / 首根完整 K 线开盘 − 1) × 100`；默认涨幅达到阈值做空，跌幅达到阈值做多；`threshold_inclusive=false` 时涨跌幅绝对值必须严格大于阈值。公布时冻结方向，公布+delay 向后对齐到可用开盘执行。
- 跨越回看边界的半根 K 线不纳入历史计算，粗粒度会缩短实际采样区间；公布、延迟和持有时限均受 K 线精度约束。零预热意味着回看数据必须存在于本次主行情范围内；不自动补取 start_at 之前的数据。
- 止损/止盈由 K 线高低价触发，随后下一根开盘市价退出，不承诺按止损/止盈价成交。持有时长从实际入场开盘计，首个收盘达到时限后下一开盘退出；同根触及止损/到期/止盈时依次优先。沿用既有逐仓清算及引擎资不抵债结算。
- 原生保护单出场根禁止再入场；在该根到达的公布事件、到期的延迟入场事件及尚未消费的等待突破事件，记 `skipped_in_position / native_exit_no_reentry`，不会延后补做。该状态沿用持仓重叠语义，不新增 status；macro/funding 共用补记入口，liquidation 扩展其日值决策队列。已有 status 的事件不覆盖，缺失 status 的最终对账仍 fail-closed。
- 每事件最多一笔。公布时已有持仓或在途订单则跳过；延迟入场时再次检查。多个等待突破的 H1 事件按事件先后处理，一旦有订单/持仓，其余等待事件跳过，不在平仓后补做。
- H2 在有效事件的公布决策时先处理 null，记 `skipped_null / missing_expected_or_actual`，再检查持仓重叠。方向映射 none、差值不足阈值分别记录原因。
- 事件早于首根开盘、处于数据缺口、或达到/晚于末根开盘+period，沿用 EVENT0 的 out-of-range 跳过口径。引擎最早可排队入场为第 3 根开盘；此前、回看不足/有缺口、计划持有窗口超出数据尾部均跳过。H1 一直没有突破则记 `no_breakout_before_data_end`。
- 资不抵债导致运行提前结束时，已成交事件以 result.v2 的实际退出为准；尚未执行的有效事件记 `skipped_run_ended / insolvency_break`，不会伪报已成交。

上述口径随每次结果写入 `assumptions.<模板名>`。
`raw_report.<模板名>` 包含：`events`、按状态计数的 `counts`、`skipped_count`、`skipped_null_count`、`skipped_in_position_count`、`skipped_events[{ts_utc,reason}]`。
每个 entered 事件的 entry/exit 价格直接来自 result.v2 的十进制字符串；不扩展冻结的 result.v2 trade 键集。

## 验证与交付

最终专项测试 **170 passed**（exit 0）；全仓 **9544 passed, 206 warnings**（185.98s，exit 0）。
完整输出与退出码保存于本 worktree 的 `.runtime/q18/full.log`、`full.exit`、`targeted.log`、`targeted.exit`，摘要及日志 SHA256 在 `evidence.json`。
专项测试使用手算价格、时间与方向；包含多空、阈值相等、null、重叠、多事件、50 条边界、拒因、零预热、按实际 R 定仓、跳空撤单及提前终止对账。
全仓命令：`PYTHONPATH=validator python3 -m pytest -q`。完整 stdout/stderr 重定向保存，并单独记录退出码。
所有既有 golden/逐字节基线保留；目录断言只新增工具身份，历史基线范围不要求新模板存在于过去的输出中。

待裁：由统领决定进 pin 4 还是 pin 5。PR 只创建、不合并；不 force push、不打 tag、不改 pin。
