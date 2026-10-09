# 9T6：缠论第三类买点（简化版，只做多）

派单：TokenBeep `docs/reviews/2026-10-09-回测加速批/tasks/9T-6-缠论三买-派单.md`；需求第45条。起点 `origin/main b42210ba1f47a2dcd98c9e2abbf9a0754088a5e5`，工作区起始干净，分支 `feat/1009-cx2-9t6-chan-3buy`，不跟踪上游。只推本分支，不开 PR、不合 main。

本批直接证据是本地识别、Backtesting.py、HTTP TestClient 和仓内 validator ASGI；行情用固定数据，没有验证真实供应方取数、预发、生产或实盘。

## 工具与参数

| 工具 id | 参数 | 默认 / 范围 |
|---|---|---|
| `local.backtesting_py.chan_3buy` | `direction` | `long`；short / both 在取数前 INVALID_PARAMS |
| 同上 | `bi_mode` | `new` / `old`，默认 `new` |
| 同上 | `exchange` | 沿用 provider 默认 |
| 共用参数 | 时间层、入场过滤器、仓位、杠杆、持仓到期 | 沿用单仓模板参数；时间层 / 过滤器默认关闭 |

冻结的回抽止损和2R止盈属于模板本身。固定百分比止损止盈、ATR止损、动态止损和分批止盈覆盖会明确 INVALID_PARAMS，避免共享风控改写本批退出口径。共享持仓到期仍正常消费。

## codex-2 定

1. 识别放旁邻模块 `strategy_chan.py`；provider 只注册工具、构建策略及挂接 assumptions / raw_report。合并序列只用于识别，成交始终采用原始 OHLC。
2. 包含方向取前一独立变化方向；最初没有前一独立K线的包含串先缓冲，看到独立方向后再处理，期间不成立分型，也不回补历史信号。解析最初包含串后重新检查当前K线是否仍被包含。
3. 顶底分型的高低点比较均严格，不把相等视为分型；相反端点之间新笔至少1根独立K线，老笔保守要求至少5根独立K线，端点下标差分别≥2、≥6。相反端点必须满足相应价格方向；同向更极端端点替换并记录，较弱同向端点忽略。
4. 只用已确认笔的三笔交叠，ZG=min(high)、ZD=max(low)、ZG>ZD。新中枢必须由此前中枢结束后的三笔构成，不复用此前中枢笔，因此不做延伸/扩张；新中枢成立即替换当前中枢。已经成立的中枢和历史决策冻结，不因后续同向端点替换回写。
5. 向上离开笔高点严格>ZG，紧接回抽笔低点严格>ZG；回抽底分型右侧独立K线收盘确认时才给信号。不满足严格条件的该次机会作废。同一中枢至多一个信号机会，持仓、时间窗、过滤器或跳空阻断也不补买。
6. 止损=回抽低点×0.999；止盈=实际engine成交价+2×(成交价−止损)，均冻结。盘中Low触及止损、时间到期/定时平仓、盘中High触及止盈、收盘严格<ZG按序收盘决策，下一根开盘市价退出。阈值是触发价，未假定按阈值成交。
7. 下一开盘实际成交价（含engine价差）≤冻结止损则取消入场并记原因。尾根没有下一开盘不排新单；存量持仓仍沿用engine finalize_trades结算，assumptions披露。期货杠杆沿用单仓逐仓仲裁，并传入冻结止损比较清算顺序。
8. 明细仅进raw_report.chan，注明预热+主区间零起下标；result.v2键集不变。时间层/过滤器显式关态必须保持engine、result.v2、assumptions、raw_report指纹，过滤器旧58条只追加4条。

## 手算与测试

独立手算序列顶1=111、底3=79、顶5=106、底7=89，三笔中枢ZG106、ZD89，在8确认；离开顶9=131，在10确认；回抽底11=114，在12确认。第11根收盘还没有信号；第12根收盘决策，第13根原始开盘125成交。冻结止损113.886，止盈147.228；14根High147不能止盈，15根High148触发，16根开盘141退出。若按信号收盘122错误计算目标，会得到138.228，测试检出提前退出。

67个独立用例覆盖包含两方向与初始未知方向、真实相邻分型间隔、同向端点替换、笔/中枢确认时刻、ZG/ZD、回抽严格边界、同一中枢消费、新中枢不复用旧笔、全前缀与未来尾部变异、预热点与主区间成交、入场跳空、实际成交价冻结2R、止损/时间/止盈/信号优先级、ZG收盘相等边界、时间/过滤器阻断、尾根、old模式、非法参数取数前拒绝、HTTP原始明细与result.v2键集。期望常量来自手算，识别期望不调用被测函数生成。

同一中枢重复消费用例固定当前中枢状态，单独检验消费仲裁；另有真实识别流的新中枢不复用旧笔用例。提前使用底分型的变异把原本在右侧确认收盘的信号移到前一根，实际交易用例要求成交13，故能发现提前成交12。

过滤器基线JSON旧58条逐值不变，新增该工具默认/显式long × 有/无预热4条；时间关态单独冻结在 `9t6_chan_off.json`。`capture_off.py` 只采集兼容指纹，不生成识别/交易的手算期望。通用用例对新工具删除冲突的百分比退出覆盖；动态退出改验明确拒绝，持仓到期仍验真实成交。历史e25886e没有本工具，新工具L=1当前默认比较作为关态补证，独立指纹由时间/过滤器兼容提供；其他工具原断言不变。

## 验证记录

| 门 | 结果 | 真实 EXIT |
|---|---|---|
| 起始provider基线 | 5102 passed / 114.60s | 0 |
| 原始b42210b完整树基线（含validator） | 5131 passed / 88.51s | 0 |
| 定向+时间/过滤器/通用兼容 | 1744 passed / 49.01s | 0 |
| 最终独立定向 | 67 passed / 1.74s | 0 |
| 收尾provider+validator | 5230 passed / 87.23s | 0 |
| 仓内validator | 29 passed / 1.30s | 0 |
| 本工具真实provider本地ASGI validator冒烟 | ok=true，30个catalog工具，errors=[] | 0 |

**5230 = 5131基线 + 67独立用例 + 32注册兼容新增**；仅provider为 **5201 = 5102 + 67 + 32**，validator为29。32=时间关态/窗4+过滤器10+杠杆10+持仓时间1+L=1仲裁关态3+风险特征4。原始树完整基线通过git archive b42210b放到 `/tmp/9t6/baseline-tree/` 执行，避免临时覆盖当前代码；起始provider基线在任何实现编辑之前执行。定向兼容1744条是在56个独立用例阶段执行，后续增加的11个独立用例由最终67条定向及5230全量覆盖。

日志均在 `/tmp/9t6/`，`checks.json` 留真实退出与日志hash。开发初次定向2失败来自消费用例未隔离新中枢发现、以及误写历史result.v2字段名；修正用例后通过，原失败日志保留。没有删旧用例、skip或xfail。

六条变异（`mutations.py`、`mutations.json`）：不做包含4红，跳过新笔间隔3红，ZG取max1红，回抽允许相等1红，未确认底分型1红，同中枢重复信号1红。均compile-valid、变异EXIT=1；逐条备份还原、cmp=0、全67条复验EXIT=0。还原SHA256：`481f8e717f8772fd25b84651bbe214e2974b6cff4527850eef7800565590c0c3`，备份 `/tmp/9t6/strategy_chan.before-mutations.py`。未用git checkout还原。

可复用命令（仓库根目录，无测试输出管道）：

```sh
python3 -m pytest backtesting-py/tests/test_9t6_chan_3buy.py -q -p no:cacheprovider > /tmp/9t6/focus-final.log 2>&1; echo "EXIT=$?"
python3 docs/reviews/2026-10-09-9T6/mutations.py > /tmp/9t6/mutation-run.log 2>&1; echo "EXIT=$?"
PYTHONPATH=validator python3 -m pytest backtesting-py/tests validator/tests -q -p no:cacheprovider > /tmp/9t6/all.log 2>&1; echo "EXIT=$?"
PYTHONPATH=validator python3 -m pytest validator/tests -q -p no:cacheprovider > /tmp/9t6/validator.log 2>&1; echo "EXIT=$?"
python3 docs/reviews/2026-10-09-9T6/validator_smoke.py > /tmp/9t6/validator-smoke.log 2>&1; echo "EXIT=$?"
```

提交交付：中文分主题提交；正常提交成功，未绕过钩子；分支尾注 `Co-Authored-By` / `Claude-Session` 计数0。只推本分支，不配置上游。
