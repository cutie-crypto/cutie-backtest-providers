# PROV-TRADECOUNT-P 审查结论：PASS

结论是 PASS，没有阻断、高或中严重度的问题。有 1 条低严重度的测试建议和 1 条需要核对的事项，都不影响合入。

本次审查只依据你给的完整差异、新测试和相关生产代码。我这边没有 shell 或读文件工具，所以**没有在 worktree 里跑测试，也没有读材料以外的文件**。下面关于测试是否通过的判断都是推断，不是实测结果。

## 逐项核对

- **正确性和口径**
  - `portfolio_result_v4.py:166-167` 用 `str(sum(f["side"] == "sell" for f in events))` 计数。`events` 是前面已经逐条校验过的成交，`side` 只能是 `buy` 或 `sell`。
  - 账本每次 rebalance 对每个币最多产生一条卖出成交。部分减仓算一笔；跨多个 FIFO lot 的一次卖出，在 `_execute` 里也只追加一条成交，同样算一笔。这和本单定的口径一致。
  - 买入成交和期末仍未平的仓位都不计入。
  - 回滚走 `deepcopy` 暂存再提交，失败的那一期不会留下成交，所以计数不会多。
- **类型**
  - `str(int)` 只会得到 `0` 或不带前导零的非负整数，符合 server 的「非负整数字符串」要求。
  - 成交上限是 10000，不存在溢出或格式问题。
  - 非 v4 结果的路径没有动。
- **没有影响信号、取数和其他指标**
  - 三个测试文件和金样里，原有的 `total_return`、`max_drawdown`、`fill_count`、现金、净值和成交全部没变，只是在等值断言里加了 `trade_count`。这本身就能说明执行路径没有变化。
- **共用出口覆盖**
  - 共享 runner、周轮动和 BTC.D 轮动都经过 `assemble_result_v4`。
  - HTTP 成功回包同时通过 `**result` 和 `raw_report`（按 `RAW_REPORT_V4_KEYS` 取的 `metrics`）带出 `trade_count`。测试也断言了 `raw_report['metrics'] == body['metrics']`。
  - `portfolio_rotation_http.py:53` 目录里的 `output_schema.metrics` 已经同步加上，两个工具在测试中都有核对。
- **金样**
  - `portfolio_10d_golden.json` 只加了 `"trade_count": "2"`，和 `fill_count: 6` 里卖出成交的数量对得上；`test_v4_outlet_closed_count_string` 对 runner 和组装两个出口都做了交叉核对。
  - 周轮动 8 笔成交里 4 笔卖出，BTC.D 4 笔里 2 笔卖出，都和手写数据一致。
- **范围外**
  - connector 的三键校验会拒绝新字段，这一点按你的要求只做了只读确认，**不算本次实现的缺陷**。

## 发现

**[低] 部分卖出加多 lot 的测试，分不清「卖出成交笔数」和「完全平掉的 FIFO lot 数」**

位置在 `backtesting-py/tests/test_trade_count_outlets.py:67-71`。

在 `['.2', '.4', '.3', '0']` 这组数据里：

- 卖出成交是 2 笔：卖 1、卖 3。
- 完全平掉的 lot 也正好是 2 个：lot1 和 lot2 都在卖 3 那一步清零。

所以如果有人把实现改成「数完全平掉的 lot」，这个用例照样通过。注释里只排除了「4 笔成交」和「3 个 FIFO 片段」两种错误口径，没排除这一种。

建议在参数里加一组 `(['.2', '.4', '.3'], 1)`：只做一次部分减仓、没有 lot 被平掉，期望是 1。加上后，「部分卖出也算一笔」就有了独立的锚点。

另外，`:64` 的 `expected == sum(f['side'] == 'sell' ...)` 用的是和实现同一个公式，起独立作用的只有写死的期望值（2、4、2）。这只说明这一行的检测力有限，不算缺陷。

## 需要核对（不算本次缺陷）

- **目录 `output_schema.metrics` 从三项变成四项。** 需要确认 server 和 connector 读取目录时，有没有对 v4 的 metrics 列表做严格相等校验。你提供的材料只说了 server 在**结果**里接受这个可选字段，没有涉及**目录**。如果目录侧也有严格校验，就和上面那个 connector 缺口一样，属于范围外的消费者问题，不算本次实现的缺陷。
- **`test_all_success_outlets_have_trade_count` 依赖 `parents[2]/'freqtrade'/...` 这个仓库目录结构，并且会 `exec_module` 一个真实的 provider 模块。** 换个目录结构或在 CI 里跑可能会出问题，建议在本地跑一次全量确认。我没有实测过这一点。
