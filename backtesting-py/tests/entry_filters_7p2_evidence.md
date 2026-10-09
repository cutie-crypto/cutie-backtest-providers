# 7P-2 多周期过滤：本地交付证据

基线 `2277d15d2f306edc612edfdfbe0deb13634488ea`，从 `origin/feat/1009-cx4-7p-filters` 建立不跟踪上游的新分支 `feat/1009-cx4-7p2-mtf`。
仅扩展既有单仓过滤层；不接账本、kernel_v3、独立 signal_execution，不改服务端、实盘判定器或部署。

## 参数与口径（codex-4 定）

| 参数键 | 缺省 | 约束／意义 |
| --- | --- | --- |
| filter_timeframe | 空字符串 | 三过滤器共用；空值沿用主周期，显式值必须严格大于主周期 |
| filter_layer_enabled | false | 总开关；开启时至少选一个子过滤器 |
| filter_ema_enabled / filter_ema_period | false / 200 | Close > EMA；周期整数 2–500 |
| filter_macd_enabled / filter_macd_fast / filter_macd_slow | false / 12 / 26 | DIF > 0；快 2–100、慢 3–300，快 < 慢 |
| filter_supertrend_enabled / filter_supertrend_atr_period / filter_supertrend_multiplier | false / 10 / 3 | trend == +1；ATR 整数 5–30，倍数 1–6 |

- codex-4 定：过滤周期共用一个键，支持既有执行周期的固定集合 `1m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w`；月周期不接受。显式相同／更小周期、非法类型／值，关闭却提供非缺省周期均取数前 `INVALID_PARAMS`。
- codex-4 定：主周期决策时刻为开盘时间 + 主周期长度；大周期收盘时间 ≤ 决策时刻才可见（相等可见）。周线复用仓内周一 UTC 网格。入场仍由模板 BUY → 时间门 → 过滤门 → 下一主周期开盘成交；尾根无下一开盘禁止新入场。
- codex-4 定：首个主周期决策前（含相等边界）必须已有 required_bars 根完整大周期历史，连续覆盖至最后决策可见的大周期根；缺首根、内根、尾根、错位、非有限值、空数据或取数异常均 `INSUFFICIENT_DATA`。不补洞、不退化为主周期。
- codex-4 定：大周期指标预热与主周期模板／风控预热分离，按主 K 线索引读取已对齐掩码，主周期预热长度不改变大周期判定。只在启用且指定大周期时增加取数；EMA／MACD／Supertrend 继续 AND，只做 long 入场。
- short/both 在取数前拒；账本／kernel_v3 连显式缺省新键也拒；关态保留旧交易、权益、完整 result.v2、assumptions 与 raw_report 字节。启用态周期／历史跨度／来源只增加到 raw_report.entry_filters，result.v2 键集不变。
- 7-P4 起（provider `1d66844` 之后）：short 改为严格镜像放行（close < EMA、DIF < 0、trend == -1，相等两边都不放行，多周期同口径透传方向），只有 both 仍在取数前拒；上两条里「只做 long 入场」「short/both 在取数前拒」是 7P-2 交付时的历史口径。

## 取数量级（本地推导，无真实网络调用）

固定 365 天、网格对齐的 15m 主周期 + 4h EMA200 过滤，取数身份相同（交易所／市场／币种）。

| 层 | _fetch_ohlcv 逻辑调用 | 根数 | 无缓存 Binance 分页量级 |
| --- | --- | --- | --- |
| 15m 主区间 | 1 | 35,040 | 36 页（每页上限 1,000） |
| 15m 模板预热（缺省 ema_cross） | 1 | 请求 63、保留 61 | 1 页 |
| 4h 过滤整段含预热 | 1 | 2,390 = 2,190 + 200 | 3 页 |

启用过滤不随主周期逐根取大周期；多个子过滤器共用该一次请求，历史根数取各指标 required_bars 最大值。
按当前中心取数 90 天分片函数：主区间 5 片、主预热 1 片、大周期含预热 5 片；缓存、实际服务端接受情况、中心→交易所回退与重试会影响真实网络次数，本批未测这些链路。
全年根数／一次逻辑请求／当前分片数由独立用例直接验证，分页量级由现有 ccxt 上限静态推导。

## 验证与算式

日志都在 `/tmp/7p2/`，完整输出重定向，无管道，真实退出码如下：

- baseline.log：4179 passed, 30 warnings in 75.09s；EXIT=0。
- targeted.log：734 passed, 2 warnings in 24.18s；EXIT=0。
- final.log：4321 passed, 30 warnings in 80.70s；EXIT=0。
- validator.log：29 passed in 0.48s；EXIT=0（validator 全量测试，与 provider 门分开计数）。
- 收尾全量：4179（基线）+ 142（新增：独立新文件 136 + 既有未接模板新增键拒参枚举 6）= 4321；无新增失败或跳过。

provider 基线／收尾命令在 `backtesting-py/`：
`python3 -m pytest tests -q -p no:cacheprovider > /tmp/7p2/<baseline|final>.log 2>&1; echo "EXIT=$?"`。
定向命令在仓库根：
`python3 -m pytest backtesting-py/tests/test_entry_filters_mtf.py backtesting-py/tests/test_entry_filters.py -q -p no:cacheprovider > /tmp/7p2/targeted.log 2>&1; echo "EXIT=$?"`。
validator 在 `validator/`：
`python3 -m pytest tests -q -p no:cacheprovider > /tmp/7p2/validator.log 2>&1; echo "EXIT=$?"`。

首轮定向夹具沿用引擎直调参数，低于 HTTP 目录约束，修正新夹具后全绿；未放宽产品目录门。全年分片初始期望误把目录 max_bars 当成取数分片上限，按当前分片代码修正为 5／5；取数量级只标本地推导。

## 独立用例与变异自证

- 三过滤器独立手算：EMA2 由 100 跳 110 得 106 2/3，DIF(2,3) 得 5/3，Supertrend(5,1) 越过旧上轨 102；19:45 不可见，20:00 与 20:15 可见。修改未收盘大周期 Close 不改变此前判定。
- 覆盖大周期 AND、无效未来尾段裁掉、周一周线边界、严格缺历史、非法周期、short/both、排除模板、请求隔离；三个过滤器 × 有／无主预热走 HTTP 并检查入场时刻；全部 22 单仓模板走注册 HTTP 路径。
- 58 组显式缺省周期关态全字节指纹；既有全部关态、风险组合与过滤入口测试继续绿。
- 变异 7 条：未收盘大周期可见、收盘 ≤ 改 <、缺历史退化主周期、关态仍取大周期、主预热错移大周期掩码、相同周期错误放行、做空静默放行。
- 每条真实 py_compile EXIT=0 → 对应测试 EXIT=1 → 备份还原 → cmp EXIT=0 → 同组复测 EXIT=0。
- 脚本 `/tmp/7p2/mutations.py`，汇总 `/tmp/7p2/mutations.log` 与 `/tmp/7p2/mutations/results.json`；逐条备份／日志在 `/tmp/7p2/mutations/<名称>/`。
- 收尾再 cmp 两个产品源文件与备份均 EXIT=0；静态 AST 比对 `_risk_sell`、`_risk_check_exit`、`_risk_layer_check_exit` 与基线一致，结果 `/tmp/7p2/static.json`。

仅本地直接／静态证据。未作真实行情源、舰队、Pre、生产或服务端联合验收。
提交和推送指定分支；不开 PR、不合 main、不部署。提交尾注审计结果见最终回报。
