# 9T4：MACD / RSI 底背离，只做多

基线为 `origin/main b42210ba1f47a2dcd98c9e2abbf9a0754088a5e5`，工作区起始干净；分支 `feat/1009-cx2-9t4-divergence`，不跟踪 main、不叠 9T1–9T3。只推本分支，不开 PR、不合 main。本批为本地 provider 代码与 HTTP TestClient 证据，未做市场数据供应方、预发、生产或实盘验证。

## 工具与参数

| 工具 id | 专属参数（默认；范围） |
|---|---|
| `local.backtesting_py.macd_bullish_divergence` | `fast=12` [2,100]，`slow=26` [3,300]，`signal=9` [1,100]，`compare=dif` [dif,hist]；fast < slow |
| `local.backtesting_py.rsi_bullish_divergence` | `rsi_period=14` [2,300]，`rsi_first_below=35` [0,100]，`rsi_exit_above=70` [0,100]；first < exit |
| 两者共用 | `direction=long`（short/both 取数前 INVALID_PARAMS）；`swing_n=5` [1,500]；`min_gap_bars=5`、`max_gap_bars=60` [1,5000]，含端点且 min≤max；`exchange` 沿用 provider 默认 |

时间层、入场过滤器默认关闭；仓位、杠杆与共享持仓到期键沿用单仓模板规则。模板自行冻结 L2 止损与 2R 止盈，拒绝显式固定止损止盈及非零动态退出覆盖，避免互相改写。显式关态参数保留 engine trades/equity、result.v2、assumptions 与 raw_report 的字节值。

## codex-2 定

1. MACD 在 L2 确认当根即可用该根上穿；提前发生的上穿不能补用。确认当根新低点先废除旧候选，再评估新相邻两点。
2. MACD 采用现有模板 `adjust=False` EMA 递推；hist = 2×(DIF−DEA)。RSI 复用现有 Wilder EWM 实现，中性缺失值 50 不视为成熟低点指标。
3. L1 指标也必须预热成熟：MACD slow×3+signal+1 根，RSI period×3+1 根。min_bars 在此基础上加 2N+min_gap；取数预热则加 2N+max_gap，覆盖区间起始附近最远候选的 L1 指标历史；预热+主区间共用零起下标，raw_report 明示。
4. 一对低点只产生一次信号机会；持仓、时间窗、过滤器或跳空阻断均不延迟补买。MACD 等待期间收盘严格低于冻结止损或新低点已确认则作废；等于止损不作废。
5. 止损为 Low(L2)×0.999；止盈为真实 engine 成交价+2×(成交价−止损)，成交时冻结。止损、时间到期/定时平仓、止盈、指标信号按序判定，触发后下一根开盘市价退出。
6. 跳空成交价≤冻结止损时取消入场并记原因；尾根缺少下一根开盘不排新单。数据尾部存量持仓仍沿用 engine finalize_trades 结算并在 assumptions 披露。
7. 只做多适用于现货及期货请求；已有 futures L>1 逐仓仲裁复用冻结止损参与判断，非跳空且更近的止损先于清算，其他情况清算优先。
8. 所有整型策略参数严格拒绝 bool、字符串及整数浮点。RSI first阈值 < exit阈值；间隔界限有序。

## 独立期望与兼容证据

手算序列 L1=13（Low79/Close80），L2=21（Low78/Close90），N=2 => 确认23，成交24。MACD(2,3,2)：L1 DIF=−55/18，L2 DIF≈−0.5218417318138364；L1 hist=−35/27，L2 hist≈−0.0848256276919781；22仍 DIF≤DEA，23上穿。RSI2：L1=0，L2≈7.511737089201878。23收盘94，但24开盘100 => 止损77.922、目标144.156；按信号价错误计算会得到126.156，25根 High144不能止盈，26根 High145才触发，27开盘退出。所有期待值为固定手算常量或标量递推说明，未调用被测函数生成。

`test_9t4_divergence.py` 还覆盖未确认第二低点、前缀及未来尾部不影响历史、低点根与确认根取值冲突、相邻低点、间隔5/60端点、严格背离方向/阈值、MACD 两项作废、实际成交价 R、跳空取消、止损/时间/止盈/信号优先级、时间窗与过滤器阻断、HTTP 取数前拒绝与冻结 result.v2 键集。

已有 filter 基线58条逐值不变，仅追加两工具及显式long方向的8条。新增时间关态基线单独存 `9t4_divergence_off.json`。采集脚本 `capture_off.py` 使用原 `capture_entry_filters_5b8320a.py` 方法；执行不是用例期望生成。注册枚举自动新增关态、过滤器、时间层、杠杆用例；通用 gate 用例仅对这两个新工具适配：删除冲突的旧百分比退出参数；动态退出改断言拒绝、持仓到期仍用实际成交断言；历史 e25886e 基线不含新 id，因此这两个工具的 L=1 比较用当前默认调用，独立冻结指纹由新增 fixtures 负责。其他工具原断言保持。

## 验证记录

| 门 | 结果 | 真实 EXIT |
|---|---|---|
| 基线 provider+validator | 5131 passed / 93.62s | 0 |
| 定向+时间/过滤器兼容 | 1240 passed / 44.65s | 0 |
| 通用兼容修复复验 | 564 passed / 11.85s | 0 |
| 最大间隔预热请求复验 | 94 passed / 1.66s | 0 |
| 收尾 provider+validator | 5289 passed | 0 |
| 仓内 validator | 29 passed / 0.50s | 0 |

算式：**5289 = 5131 基线 + 94 背离独立用例 + 64 注册枚举新增**；只计 provider 则 **5260 = 5102 + 94 + 64**，validator 为29。64 = 时间关态/窗8 + 过滤器20 + 杠杆20 + 时间持仓2 + L=1仲裁关态6 + 风险功能8。无 skip/xfail、旧用例未减。

7 条变异均 compile-valid、断言失败 EXIT=1，逐条还原复验 EXIT=0、cmp=0：未确认波段6红；确认根取指标6红；反向价格8红；RSI阈值2红；收盘失效1红；信号收盘价算R 2红；新低点失效1红。明细 `mutations.json`；备份 `/tmp/9t4/strategy_divergence.before-mutations.py`。还原 SHA256 `1197f9361abf6e898a40a58e58cf6675d0bbf38db84a15fae8983032100da2a7`。

保留早期失败：全量首次15 failed来自新工具注册后旧通用用例的退出/历史id/持仓序列假设，修复后完整复验；独立 validator 首次25 passed/4 failed命中本机 site-packages 旧安装包，随后 `PYTHONPATH=validator` 使用本仓实现29 passed，未改 validator 代码。94用例开发期的收集/手算/序列化问题也保留日志，没有作为通过证据。`checks.json` 记录日志 SHA256 与真实退出状态。

尾注检查与交付：正常提交钩子通过；本分支提交尾注 Co-Authored-By / Claude-Session 计数0；只推自己的分支。

可复用命令（仓库根目录，无管道，全部输出留 `/tmp/9t4/`）：

```sh
python3 -m pytest backtesting-py/tests validator/tests -q -p no:cacheprovider > /tmp/9t4/baseline.log 2>&1; echo "EXIT=$?"
python3 -m pytest backtesting-py/tests/test_9t4_divergence.py backtesting-py/tests/test_time_layer_compatibility.py backtesting-py/tests/test_entry_filters.py -q -p no:cacheprovider > /tmp/9t4/focus-final.log 2>&1; echo "EXIT=$?"
python3 docs/reviews/2026-10-09-9T4/mutations.py > /tmp/9t4/mutation-run.log 2>&1; echo "EXIT=$?"
PYTHONPATH=validator python3 -m pytest backtesting-py/tests validator/tests -q -p no:cacheprovider > /tmp/9t4/all.log 2>&1; echo "EXIT=$?"
PYTHONPATH=validator python3 -m pytest validator/tests -q -p no:cacheprovider > /tmp/9t4/validator-local.log 2>&1; echo "EXIT=$?"
```
