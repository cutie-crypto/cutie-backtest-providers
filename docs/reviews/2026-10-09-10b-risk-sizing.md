# 第 10 批 B：按风险定仓与复利（provider）

起点：origin/main `b42210ba1f47a2dcd98c9e2abbf9a0754088a5e5`；分支：`feat/1009-cx4-10b-risk-sizing`，无 upstream。只做 provider，未修改服务端、两端界面、海龟或组合内核。

| 参数 | 值与默认 | 语义 |
| --- | --- | --- |
| position_size_risk_pct | 数字，0 < x <= 100，无默认 | 第三种互斥模式，1 表示风险预算 1% |
| compound | 布尔，新路径默认 false | false 初始本金；true 成交前扣费、滑点、平仓结算后的净值 |
| position_size_qty_step | 数字 >= 0.00000001，默认 0.00000001 | 用户数量下取步长；没有声称是受信交易所规则 |
| position_size_pct / position_size_notional | 沿用旧参数 | 与 risk_pct 三选一；显式新字段使其进入新路径 |

新三字段全省略时，旧路径、旧复利行为和原结果字节不变。任一新字段出现时必须且只能提供一种仓位模式；固定名义金额不随复利改变。风险模式可用固定百分比、ATR 或初始移动止损。未接入的七个 runner 显式列于 `POSITION_SIZING_UNWIRED_TOOLS`，覆盖断言避免新增 runner 漏拒。

## 实现与自审

- `backtesting-py/strategy_position_sizing.py:16`：互斥参数解析；`:50`：Decimal 手算与下取；`:66`：请求内成交前 hook、冻结状态、拒绝记录。
- `backtesting-py/strategy_risk_overlay.py:36`：拆出初始止损计算，3a 的 R 与风险定仓复用同一冻结状态。
- `backtesting-py/cutie_backtesting_provider.py:2673`：新路径安装 sizing 并复用结算 hook；`:2812`：退出层复用冻结状态；`:5674`：未接名单；`:7311`：取数前拒绝；`:7508`：一引擎单位对应 qty_step；`:7733`：raw_report。
- `backtesting-py/tests/test_10b_risk_sizing.py:1`：66 个新增手写期望用例，含真实 EMA builder HTTP 路径、长短跳空、费用与滑点后的复利、冻结 R/动态止损时间、末根无下一开盘、七个未接 runner。
- `README.md:66`：参数、兼容、公式、成交与费用语义。
- 自审逐读了成交时钟、数量尺度、保证金/费用、结算先后、3a 冻结状态、拒绝报告及未接 runner；没有修改依赖库。

## 验证证据

所有实际命令输出均重定向到 `/tmp/10b/*.log` 并回显真实 EXIT；没有将测试接到管道。

| 命令 | EXIT | 判据 |
| --- | --- | --- |
| `python3 -m pytest -q`（基线） | 0 | `/tmp/10b/baseline.log`：5191 passed, 59 warnings in 91.62s |
| `python3 -m pytest backtesting-py/tests/test_10b_risk_sizing.py backtesting-py/tests/test_risk_overlay_compatibility.py backtesting-py/tests/test_risk_layer_3a_fingerprint.py -q` | 0 | `/tmp/10b/targeted-final.log`：721 passed, 2 warnings in 23.81s |
| `python3 -m pytest -q`（收尾） | 0 | `/tmp/10b/final-full.log`：5257 passed, 59 warnings in 88.50s |
| validator CLI（真实 app / 本地合成 OHLCV / 真实 EMA builder） | 0 | `/tmp/10b/validator-final.log`：PASS，tools_checked=29，all checks passed，smoke produced success |
| `git diff --check` | 0 | `/tmp/10b/diffcheck.log` 为空 |

N = 5191 既有 + 66 新增 = 5257；定向 N = 655 既有兼容/3a 指纹 + 66 新增 = 721。旧指纹固定金额/百分比全部走原样 fixture，无重采、无放宽。初始基线命令曾错写不存在的 `freqtrade/tests`（EXIT=4，无测试执行），随后使用仓库根全量；该准备失误不计为基线成功。定向首轮暴露冻结状态时间字段接法问题（13 failed），修复后全部绿；最后一次全量在所有变异还原后执行。

validator 完整命令与合成入口在 `/tmp/10b/validator_app.py`、`/tmp/10b/validator-final.log`；命令：

```sh
CUTIE_BACKTEST_PROVIDER_TOKEN=10b-local-validation PYTHONPATH=validator python3 -m cutie_backtest_provider_validator --app /tmp/10b/validator_app.py --token 10b-local-validation --tool-id local.backtesting_py.ema_cross --start-at 1767225600 --end-at 1767261600 --smoke-params '{"position_size_risk_pct":1,"stop_loss_pct":2,"risk_layer_enabled":true,"position_size_qty_step":0.5,"ema_fast":2,"ema_slow":3}' > /tmp/10b/validator-final.log 2>&1; echo "EXIT=$?"
```

这是本地合成数据的 HTTP 契约验收，不是实盘、真实行情取数、服务端或界面验收。

## 五项变异

`/tmp/10b/mutations.sh` 使用 `cp` 备份及还原，每次 `cmp` 的 RESTORE_EXIT=0；整体脚本 EXIT=0 表示五个负例均成功。每个 pytest 的 EXIT=1：

| 变异 | 红例 | 日志 |
| --- | --- | --- |
| 距离改用信号价 | long/short 跳空，2 failed | mutation-1.log |
| 复利开关取反 | 两笔交易，2 failed | mutation-2.log |
| 杠杆乘数量 | L=5，1 failed、L=1 passed | mutation-3.log |
| 删除无止损取数前拒绝 | 无止损请求，1 failed | mutation-4.log |
| 省略字段误进新路径 | 固定金额/百分比旧指纹，2 failed | mutation-5.log |

第五项首轮写法造成收集错误（EXIT=2），不计为断言证据；修正变异范围后得到上述 2 failed。还原源文件与备份 SHA256 一致：`684c2cdfa35223c30fc9c9037e6a7d2b7e4f7cababa598697d795b04e1695093`。

## codex-4 定

1. 第三模式字段用 `position_size_risk_pct`；与旧两个字段互斥，不新增冗余 mode 枚举。
2. 任一新字段出现才启新路径；新路径要求显式选一种定仓方式，compound 默认 false；省略三新字段保留旧复利。
3. 数量步长用 `position_size_qty_step`，默认/最小 1e-8，与结果数量精度一致；步长来自调用方，未声称交易所验证。
4. 初始止损在实际成交点计算并冻结，ATR 只看信号及此前闭合 K 线；风险数量不乘杠杆，杠杆只改变保证金。
5. 没有止损取数前 INVALID_PARAMS；成交点止损无效/未预热、距离不正、数量不足、费用后资金不足则取消该笔入场并记录原因，回测可继续；禁止自动缩水。
6. 复利净值先结算前笔费用与滑点；固定名义金额始终固定；末根信号无下一开盘则拒绝。

交付边界：只推本分支，不开 PR、不合入 main、不部署。提交与远端证据在最终回报中给出。
