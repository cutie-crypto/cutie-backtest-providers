# 7P-1 单周期开仓过滤器：本地交付证据

基线 `5b8320a87116eaa0fd36c36ae277289bb502b8e8`；分支 `feat/1009-cx4-7p-filters`。
仅单仓模板；账本、kernel_v3、多周期、服务端、实盘判定器与部署不在本段。

## 参数与口径（codex-4 定）

| 参数 | 缺省 | 范围／意义 |
| --- | --- | --- |
| filter_layer_enabled | false | 总开关；开态至少选一项 |
| filter_ema_enabled / filter_ema_period | false / 200 | Close > EMA；周期整数 2–500 |
| filter_macd_enabled / filter_macd_fast / filter_macd_slow | false / 12 / 26 | DIF > 0；快 2–100、慢 3–300，快 < 慢 |
| filter_supertrend_enabled / filter_supertrend_atr_period / filter_supertrend_multiplier | false / 10 / 3 | trend == +1；ATR 整数 5–30，倍数 1–6 |

- AND 组合；价格等于 EMA、DIF 等于零都不通过。Supertrend 复用现有 Wilder ATR／上下轨递推，不改既有模板定义。
- 总开关关闭但任一值非缺省、子过滤器关闭却修改其参数、未知 filter_ 键、非法类型／范围均取数前拒参；整数不接受 bool 或浮点数。
- 只做 long 入场；启用过滤且 short/both 拒参。CCI+RSI 缺省 both 同样拒参，需要显式 long。未接模板收到任何过滤键（含 false）拒参。
- 入场顺序：模板 BUY → 时间门 → 过滤门 → 风控准备／仓位 → 下一开盘成交。被拒信号不缓存、不延迟重放；保留原模板自身的触发消费状态。
- 过滤数组只在启用态计算，不注册新引擎指标、不改模板启动根；EMA 所需根数为周期，MACD 为慢周期×3，Supertrend 为 ATR 周期+1。与原模板／ATR 共用一次预热请求；总历史不足报 INSUFFICIENT_DATA，否则历史足够前不放行。
- 启用态裁掉未在 min(end_at, now) 收盘的主区间 bar；末根无下一开盘禁止新入场。固定 m/h/d/w，月周期拒参；独立 signal_execution 路径尚未消费过滤层，组合请求取数前拒参。
- 仅启用态 raw_report.entry_filters 披露规则；result.v2 键集与平仓仲裁不改。

## 直接本地证据

- 修改前保存不可变 fixture：22 个单仓模板、全部目录方向变体共 29 个配置 × 两种预热状态 = 58 组；既有 fixture 未改。
- 关态用例：58 组 × 缺省/显式完整缺省键 = 116 次，守完整交易／权益 IEEE 浮点、canonical result.v2、min_bars、预热根、assumptions、raw_report；另 320 次旧风险组合指纹。
- 三过滤器各有独立手算的放行／拦下；含等值、AND、未来价格改值不影响过去。全部 22 模板 × 两种风控模式实际运行，观察统一入口调用，并用独立 EMA2 标量递推核交易入场。
- 模板平仓、止损平仓不受过滤拦截；覆盖末根、拒参前无取数、请求隔离、缺历史、end_at/墙钟未收盘裁剪和 raw_report 披露。
- baseline: 3587 passed, 30 warnings in 56.98s；真实 EXIT=0。
- targeted: 592 passed, 2 warnings in 21.58s；真实 EXIT=0。
- final: 4179 passed, 30 warnings in 75.79s (0:01:15)；真实 EXIT=0。
- validator: 29 passed in 0.58s；真实 EXIT=0。

命令分别在 backtesting-py 或 validator 目录执行：
`python3 -m pytest [tests/test_entry_filters.py] -q -p no:cacheprovider > /tmp/7p1/<名>.log 2>&1; echo "EXIT=$?"`。
日志：`/tmp/7p1/baseline.log`、`targeted.log`、`final.log`、`validator.log`。

## 变异与还原

7 条：平仓加过滤门、判定偷看下一根、关态仍计算、short 静默放行、EMA > 改 >=、BUY 绕过过滤、AND 改 OR。
每条真实 py_compile EXIT=0 → 定向 pytest EXIT=1 → 备份还原 → cmp EXIT=0 → 同组 pytest EXIT=0。
脚本 `/tmp/7p1/mutations.py`；各项日志／备份 `/tmp/7p1/mutations/<名称>/`；汇总 `results.json` 与 `/tmp/7p1/mutations.log`。

平仓两函数与 _risk_sell 的 AST 与基线相同；全仓直接 self.buy/self.sell 仅出现在共用入场方法。
6c@19faec3 的临时三方文本合并预览 EXIT=0，编译 EXIT=0；未接账本，不修改 6c 所增账本段落。此为静态兼容检查，未作合入或组合运行验收。

收尾全量算式：3587（基线）+ 592（新增）= 4179；无新增失败或跳过。
交付状态：按派单提交并推送 feat/1009-cx4-7p-filters；不建 PR、不合 main、不部署。尾注审计计数要求为 0；最终屏上报告提供实际结果。
