# F5：VWAP 偏离回归交付证据

工作树：`cx4-risk`；分支：`feat/1009-cx4-f5-vwap-reversion`；起点：`b42210ba1f47a2dcd98c9e2abbf9a0754088a5e5`，无上游跟踪起分支。初始工作区干净。包含派单「追加 1」。只交 provider 分支，不开 PR、不合 main、不注册服务端或部署。

## 工具与参数

工具：`local.backtesting_py.vwap_reversion`，只做多，spot / futures，单仓。目录保留既有工具顺序，新增工具追加末尾；目录周期 15m / 30m / 1h / 4h。执行仅允许已有执行集合中 <1d 且整除 UTC 一日的固定周期。

| 键 | 类型 | 默认 | 范围 / 语义 |
| --- | --- | --- | --- |
| `vwap_deviation_pct` | number | 1.5 | 0.5–5，正数幅度；负数 INVALID_PARAMS |
| `time_vwap_price` | string | hlc3 | hlc3 / close；可选时间层关闭时仍消费 |
| `stop_loss_pct` | 共享 number | 2（模板补充） | 用户显式给出任何风控止损 / 止盈键时，整组默认不补；共享校验规则保持原样 |

共享风控、仓位、时间与过滤器键由已有 schema 合并，杠杆沿用 1–20 且 >1 只允许 futures。不发布 direction；short / both 即使 futures 也不支持本模板。

## codex-4 定

1. **句式带负号、参数存正幅度**。入场 close ≤ VWAP × (1 − deviation/100)，出场 close ≥ 本根 VWAP；均收盘确认、下一根开盘市价。价格触碰影线不会触发 VWAP 回归退出。
2. 复用 `prefix_vwap`，周期固定 UTC 自然日，含本根累计；追加 1 的 `time_vwap_price` 直接透传。累计成交量零产生空值时不判断模板信号；已持仓仍先执行风控 / 到期。
3. 复用时间层 `expiry_due` + 午夜 `flatten_at=00:00`，独立 UTC 内在时钟提供到期事实，不打开可选 `time_layer_enabled`。信号根是 UTC 日最后一根时禁止新仓；持仓日末收盘到期，次日第一根开盘成交。统一仲裁：爆仓 → 止损 → 日终 / 配置时间到期 → 止盈 → VWAP 信号；默认止损收盘触发，风控层显式启用后按 High/Low。
4. 请求按主区间第一根回溯到当日午夜所需根数取尽力预热；保留预热时间戳，验证周期起点与连续性。覆盖不足 / 预热中有缺口就跳过首日，`raw_report.vwap_reversion.history_notes` 记录原因，下个 UTC 日重新判定。主区间缺口 / UTC 网格错位报 TIME_DATA_GAP。
5. 风控初始价格冻结于入场信号收盘。下一根真实开盘在冻结止损错侧（含相等），broker 成交前取消订单并记录 raw_report；不读取未来开盘来做信号。ATR 使用信号根已收盘历史。复用共享动态风控与杠杆结算；冻结止损在真实成交爆仓线之外时，报告按实际价位披露。
6. 数据尾部没有下一开盘时不开新仓；剩余持仓沿用引擎 finalize_trades 结算，assumptions 明示，不宣称缺失次日行情时已证明次日开盘成交。
7. result.v2 键集不变，新解释信息进 assumptions / raw_report。时间与过滤器装饰器均挂载，可选层默认关闭。

## 独立手算

`test_f5_vwap_reversion.py` 的 6h 小序列跨三个 UTC 日，其中前两日完整。

- 日一 HLC3 / 量：100/0、100/1、94/3、94/0；VWAP = 空、100、95.5、95.5。第 2 根 close94 ≤ 95.5×0.985=94.0675，第 3 根开盘94入场；该根 High100 不触发回归退出，日终到期，第 4 根开盘200卖出。
- 日二 HLC3 / 量：200/1、190/1、198/1、198/0；VWAP = 200、195、196、196。第 5 根 close190 ≤ 192.075，第 6 根开盘190买入；close198 ≥ VWAP196，第 7 根开盘199卖出。
- close 源差异：日一第 2 根 High106 / Low94 / Close94，HLC3=98，VWAP=(100+3×98)/4=98.5；close 源 VWAP=(100+3×94)/4=95.5。期望不调用被测函数生成。

## 关态指纹

- `entry_filters_5b8320a.json` 通过既有 `capture_entry_filters_5b8320a.py.snapshot` 方式，仅追加 `vwap_reversion/0`、`vwap_reversion/1`；原 58 条语义完全不变。采集函数新增预热时间戳挂载，原工具不消费，因此既有快照行为不变。
- `time_layer_single_f5_b42210b.json` 增补新模板 min_bars、assumptions、raw_report 指纹；原两份时间层金样不改。禁构造可选时钟的关态测试通过；模板内在 UTC 时钟不等于开启可选时间层。
- 新模板不存在于爆仓测试的历史基线 `e25886e`。`isolated_off_f5_b42210b.json` 独立补 spot / futures 指纹，新模板 default / 显式 leverage1 对照；原工具仍逐字节对照历史源码，没有放宽或跳过旧测试。

## 验证与日志

所有最终门真实 EXIT=0，日志在 `/tmp/f5/`：

| 门 | 日志 | 结果 |
| --- | --- | --- |
| 基线全量 | baseline.log | 5102 passed |
| F5 独立用例 | focused-development-4.log | 55 passed |
| F5 / 时间关态 / 过滤器定向 | focused-final.log | 1183 passed |
| 历史爆仓基线扩展修复定向 | compatibility-repair.log | 104 passed |
| 收尾全量 | final-full-repaired.log | 5185 passed |
| 当前 validator 测试 | validator-local.log | 29 passed |
| 当前 validator ASGI / 新模板成功路径 | validator-smoke-final.log、validator-report.json | 所有校验项 PASS；30 tools；F5 success、2 trades |

全量算式：**5185 = 5102 + 55 + 28**。28 个动态枚举扩展 = 时间关态 / 时段4 + 过滤器6 + 杠杆校验10 + 持仓时间1 + 动态风控4 + 爆仓关态3。所有既有模板金样保持不变。

命令口径：`python3 -m pytest <范围> -q -p no:cacheprovider > /tmp/f5/<日志>.log 2>&1; echo "EXIT=$?"`，无管道；validator 测试显式 `PYTHONPATH=validator`，ASGI 用 `/tmp/f5/validator-smoke.py`，加载路径确认是当前工作树源码。

开发过程保留失败：`focused-development*.log`；首次收尾全量 `final-full.log` 为 4 failed / 5181 passed（目录顺序与新模板不存在于历史源码的兼容性接入），修复定向 `compatibility-repair.log` 104 passed。首次未指定 PYTHONPATH 的 validator 使用机器上旧安装包，4 failed / 25 passed；当前源码 `PYTHONPATH=validator` 的测试 29 passed。不把旧包失败算成当前 validator 缺陷。

## 变异

`python3 backtesting-py/tests/mutate_f5_vwap_reversion.py`：10/10 编译 EXIT=0、目标 pytest EXIT=1、每次备份还原与 cmp EXIT=0。覆盖上一根 VWAP、跨日不重置、High 触碰退出、日终晚一天、1d 放行、负阈值放行、close 源被忽略、入场等号错误、错侧止损放行、首日缺历史放行。

日志：`/tmp/f5/mutations.log`、`mutations-summary.txt`、逐项日志、两份 `.f5-original` 备份。最终还原 SHA256：

- provider：`eb30259d1ba22f5b731000b61a5beb8965dcf73ff3008b596df4aa99671b29d9`
- 时间序列：`380c616f53b7374c5b1eefda5c3057867c355a26b8a21381311d1ae3045d71b5`

证据界限：本机真实 backtesting.py、注册 HTTP / ASGI、固定离线 OHLCV；没有交易所实时取数、服务器注册、真机、Pre 或生产证据。服务端后续按 S2 追加 1 处理负号句式与正幅度参数映射。
