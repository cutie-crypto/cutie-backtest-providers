# F6：连续阴线 + RSI 超卖反弹 provider 交付证据

工作树 `cx4-risk`；分支 `feat/1009-cx4-f6-red-streak-rsi`；起点 `origin/main=5b8320a87116eaa0fd36c36ae277289bb502b8e8`，无上游跟踪、不依赖 7-P。
本材料是离线实际 Backtest、FastAPI HTTP 入口与本仓 validator 的本地证据；服务端注册、舰队部署、Pre、生产和实盘未执行。

## 工具与参数

工具 `local.backtesting_py.red_streak_rsi`；只做多，现货和 futures 均可使用；不提供 direction，short / both 在 schema 校验阶段返回 INVALID_PARAMS、不会取数。其余杠杆门禁沿用基线。

| 参数 | 默认 | 范围 / 口径 |
|---|---:|---|
| red_bars | 4 | 严格整数 3–8；阴线为 Close < Open，十字星清零 |
| rsi_period | 14 | 严格整数 2–100；复用 Wilder RSI |
| oversold | 30 | 数值 1–49；本根 RSI 必须严格小于阈值 |
| max_holding_bars | 12 | 严格整数 1–1000000；模板固有出场机制，无需开启新风险或时间层 |
| stop_loss_pct / take_profit_pct | 3 / 3 | 两键均未提供且无其他定价模式时才注入整组默认；用户给任何止损 / 止盈定价键，均不补默认 |
| risk_layer_enabled | false | 关态收盘触发；开态使用 High / Low，扩展规则仍按共用校验要求开启 |
| 时间层 / 仓位 / leverage / exchange | 共用默认 | 时间层默认关闭；leverage 基线 HTTP 只放行 1 |

`min_bars=max(red_bars, 3*rsi_period+1)`，默认 43；预热取数与 RSI 收敛深度沿用存量 RSI 模板，阴线计数可包含已收盘预热段，但不补发过去的信号。

## codex-4 定

1. 入场只在连续阴线的**第 N 根**收盘判断当前 RSI。第 N 根未超卖时，不在同一连续段第 N+1 根追补入场；后续先出现非阴线才重新计数。无下一根开盘的尾根不新开仓。
2. 持仓直接复用 `_TimeLayerMixin._holding_expiry` → `strategy_time_layer.expiry_due`：成交根算第 1 根，`bar-entry_bar+1 >= N` 在第 N 根收盘触发，下一根开盘市价平仓。例如信号根 43 → 成交根 44 → 第 12 根 55 收盘触发 → 根 56 开盘平仓。
3. 止损、止盈及 R 档初始价位冻结在入场信号收盘，不以成交后的价格重新计算。仅在本模板的 broker 实例包装 `_process_orders`，于实际成交根开盘、原方法处理订单之前核对冻结止损；Open <= 冻结止损则取消尚未成交的入场，`raw_report.red_streak_rsi.skipped_entries` 记录原因、信号/执行时间、止损和开盘价。无全局 broker 修改、无前视 OHLCV 读取。此处依赖 backtesting.py 私有接口，当前版本由真实引擎与 HTTP 测试守护。
4. 默认 3% / 3% 按整组缺省处理；用户提供 stop_loss_pct、take_profit_pct、ATR、R 倍、移动、保本或分档定价键，整组默认均不注入。仓位键、时间键、risk_layer_enabled 本身不抑制模板默认定价。
5. 风控层关闭保留收盘触发口径，开启则为 High / Low；同根止损 → 持仓/时间到期 → 止盈，无模板信号出场。动态与分档处理复用共用风控层。触发后均为下一根开盘市价；尾部未到期持仓仍采用基线 finalize_trades 结算，并披露于 assumptions。
6. result.v2 的 trades / metrics / manifest 键集保持冻结，新取消入场证据只进入 raw_report，成交时机与计数在 assumptions 披露。新模板时间装饰器已接入；原模板不可变指纹继续保留，新模板另比较默认关闭与显式关闭时的 trades、权益、canonical result.v2、assumptions 和 raw_report。

## 验证

所有 pytest 均使用 `python3 -m pytest … -q -p no:cacheprovider > /tmp/f6/<name>.log 2>&1; echo "EXIT=$?"`，无管道，读取真实退出码。

| 验证 | 结果 | EXIT | 日志 |
|---|---|---:|---|
| 基线 provider 全量 | 3587 passed | 0 | baseline.log |
| F6 + 时间兼容定向 | 586 passed | 0 | targeted.log |
| F6 共用风控实际引擎四类路径 | 4 passed, 329 deselected | 0 | risk-fixture-fix.log |
| 收尾 provider 全量 | 3658 passed = 3587 + 54 F6 + 2 关态 + 15 自动枚举新增 | 0 | final-full.log |
| 本仓 validator 测试 | 29 passed | 0 | validator-local.log |
| 本仓 validator → 实际 F6 app | PASS，29 tools / 9 checks | 0 | validator-smoke.log / validator-smoke.json |

自动枚举新增 15 = leverage schema 1 + leverage 非法值 9 + 风控四类 4 + 时间层持仓 1。F6 期望值由固定根号/价位和独立 Wilder 递推确定，不调用被测函数生成期望。

首轮定向失败 2 项为夹具/字段路径错误，已保留修正过程；首轮收尾全量为 1 failed / 3657 passed，原因是共用保本用例将所有 Open 设为 Close，不存在阴线，F6 根本不能入场。只为 F6 在低位补四根真实阴线，保留其他模板原数据，四类实际引擎门和收尾全量通过；原失败日志为 final-full-first.log。
首次 validator 命令加载 site-packages 中旧安装包（工具上限 10），4 failed / 25 passed；未修改 validator 产品代码，使用 `PYTHONPATH=validator` 后 29 passed，日志分开保留。实际 app validator 使用本仓代码，OHLCV 与报告绘图为本地替身，回测与 HTTP/校验逻辑为实际代码。

## 变异与还原

每条先独立备份，再修改，`py_compile` 通过后跑全部 54 项 F6 测试；均 pytest EXIT=1，随后从备份还原、`cmp` EXIT=0。脚本及完整记录位于 `/tmp/f6/mutations.py`、`mutations.log`、`mutation-summary.json`。

| 变异 | 红用例数 | 日志 |
|---|---:|---|
| 第 N 根 RSI 取前一根 | 1 | nth_rsi_previous.log |
| 十字星算阴线 | 24 | doji_is_red.log |
| 持有根数少计成交根 | 15 | holding_off_by_one.log |
| 默认止损覆盖用户输入 | 5 | defaults_override_user_stop.log |
| RSI 等于阈值允许入场 | 2 | rsi_boundary_inclusive.log |
| 去掉成交前跳空错侧取消 | 2 | gap_guard_removed.log |
| 连续第 N+1 根追补入场 | 1 | later_red_allowed.log |

provider 还原 SHA256 `1204477cfa1277bbc4a1b913895d4927ec0feea9a820c18b69ebc81d5550de75`；共享时间层还原 SHA256 `efffcd2aece630c39c689a1a60c72d1bc54e31fec6ddf7fbd9f64e7e758c12d0`，共享时间层不在提交改动内。

提交前 `git diff --check` 通过；提交尾注 `grep -ciE "^(co-authored-by|claude-session):"` 数量 0。仅推本分支，不开 PR、不合 main；提交钩子照常执行。
