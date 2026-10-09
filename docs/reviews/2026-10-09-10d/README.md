# 10D 组合账本交付证据

基线 `origin/main=09bd963`；分支 `feat/1009-10d-portfolio-ledger`，独立工作树 `cx4-10d`。
只新增文件，不改既有 runner、内核、注册、catalog；只本地提交，不推送、不合入。
C1 读取头 `1f3bdc7d0`；C2 读取头 `cefdc9a4e`。所有读取和一次性校验均未向 TokenBeep 写文件。

## 公开接口

- `portfolio_ledger.py`：`SpotSpec(qty_step: Decimal, min_qty: Decimal, min_notional: Decimal)`；`OpenPrices(ts: int, prices: Mapping[str, Decimal])`。
- `PortfolioLedger(initial_cash: Decimal, specs: Mapping[str, SpotSpec], fee_bps: Decimal, slippage_bps: Decimal = 0)`。
- `rebalance(decision_ts: int, target_weights: Mapping[str, Decimal], next_open_prices: OpenPrices) -> list[dict]`。
- `mark(close_prices: Mapping[str, Decimal]) -> Mark`；`quantities() -> dict[str, Decimal]`；`cost_basis() -> dict[str, Decimal]`。
- `Mark.snapshot(ts: int) -> dict`；`reconcile_snapshots(initial_cash, snapshots, equity_curve, fills) -> None`。
- `portfolio_result_v4.py`：`assemble_result_v4(*, initial_cash, snapshots, equity_curve, fills, btc_benchmark, price_manifests, metric_series=None) -> dict`。
- `portfolio_runner.py`：`DailyBar(ts, open_prices, close_prices)`；`run_portfolio(*, initial_cash, specs, bars, target_weights, fee_bps, slippage_bps=0, btc_benchmark, price_manifests, metric_series=None, coin_pool=None) -> PortfolioRun`。
- `PortfolioRun` 提供 `result`、`rejections`、`remaining_cost_basis`。诊断不加入 v4 闭键集。

## codex-4 定

1. 目标金额基数为每次成交前的开盘估值 NAV；未出现的受信币目标权重为零，权重和不能超过一。
2. 同期先卖后买，目标金额固定不随成交重算；同侧按 C2 冻结 `symbols` 原顺序成交。费用不足时按该顺序缩量，非按比例重配。
3. 滑点参数采用 `slippage_bps`；参考 price 保持原始开盘，fee/slippage 分别作为现金成本扣除；手续费加滑点费率小于 10000bps。
4. 持仓按币拆分 FIFO 批次；剩余成本包括该批的开仓手续费和滑点。数量下取通过精确整数步数实现，账本金额/数量均保持 Decimal，不先近似除法再下取。
5. 财务加减乘使用与 C1 相同的 Decimal128 exact；独立有理数重放验证现金、数量和 NAV。任意差额拒绝，任何需有损舍入的中间结果拒绝，单次调仓原子提交。
6. `DailyBar.ts` 是原始每日开盘 Unix 秒和 UTC 日标签；第 i 根收盘目标在第 i+1 根原始开盘成交，close NAV 以所属日标签输出。首点保留交易前本金，末点目标无下一开盘时只记 `no_next_open`。
7. BTC 基准注入 `{ts, close_price}`，组装器计算无费用基准净值；BTC.D 注入完整冻结序列，只核验身份/哈希/覆盖并组装引用，历史可用性过滤留给 E 段。
8. 原派单称 cx4-risk 存在 venv，实查主仓与该工作树均无 `.venv`，历史记录使用 `python3`。本次沿用现有 `/usr/bin/python3`（实际 `/Applications/Xcode.app/Contents/Developer/usr/bin/python3`），provider 依赖已直接导入验证；环境矛盾已向用户说明。

## 手算与金样

`backtesting-py/tests/test_portfolio_10d.py` 顶部 docstring 是独立手算表；初始10000，fee10bps，滑点0，qty_step1，ETH/SOL 三次调仓。

| 点 | 成交 | 现金 | ETH/SOL 数量 | NAV |
|---|---|---|---|---|
| 初点 | 无 | 10000 | 0/0 | 10000 |
| 第1期 | 买ETH40@100费4；买SOL80@50费4 | 1992 | 40/80 | 10792 |
| 第2期 | 卖ETH40@120费4.8；买SOL113@60费6.78 | 0.42 | 0/193 | 11194.42 |
| 第3期 | 卖SOL193@50费9.65；买ETH34@140费4.76 | 4876.01 | 34/0 | 9976.01 |

金样为 `backtesting-py/tests/fixtures/portfolio_10d_golden.json`，所有财务期望值手写，未通过 runner 生成期望。
BTC 基准10000/11000/10500/12000；收益-0.002399、回撤0.10884083、fill_count6。
price manifest 是合成手写夹具，checksum 为测试声明值，不代表真实行情取数或验真。

## 验证命令与结果

从工作树 `backtesting-py` 运行，不接管道：

```bash
mkdir -p /tmp/10d
python3 -m pytest tests/test_portfolio_10d.py -q -p no:cacheprovider > /tmp/10d/focus.log 2>&1; echo "EXIT=$?"
python3 -m pytest tests -q -p no:cacheprovider > /tmp/10d/all.log 2>&1; echo "EXIT=$?"
```

从工作树根运行：

```bash
python3 docs/reviews/2026-10-09-10d/mutations.py > /tmp/10d/mutations.log 2>&1; echo "EXIT=$?"
python3 docs/reviews/2026-10-09-10d/check_server.py /Users/tf/Documents/bitbucket/TokenBeep > /tmp/10d/server.log 2>&1; echo "EXIT=$?"
```

最终结果记录在同目录 `evidence.txt`。变异命令整体 EXIT=0 表示四条变异各 EXIT=1 且断言红、cp还原后cmp=0、最终全部聚焦转绿，不是把红例当成功忽略。

## 孤儿扫描

只统计三个新增生产模块中 AST 调用位置，不把定义、测试、注释当引用；全部原始计数见 `orphans.txt`。
生产侧引用数为1的函数逐个说明：

- `PortfolioLedger.cost_basis`：由 `run_portfolio` 导出剩余批次成本，公开调用方也可使用。
- `PortfolioLedger.rebalance`：由 `run_portfolio` 执行逐期目标表，是 E 段的账本入口。
- `PortfolioLedger._execute`：只由原子调仓包装调用，保持内部成交路径单一。
- `_hash`：只由价格manifest校验调用，封装hash格式检查。
- `_metric_reference`：只由组装器调用，将冻结输入转换为闭键集引用。
- `assemble_result_v4`：只由新 runner 调用，是独立 v4 组装入口。

`run_portfolio` 在生产模块内调用数0：本批明确不注册工具，测试和校验脚本直接调用；这是给 E 段保留的公开入口。
构造器和 `SpotSpec.__post_init__` 是类实例化/dataclass隐式调用，不能按函数名AST计数判为孤儿；文档脚本的 `main` 为各自命令行入口。

## C1 跨仓限制

本批手写金样与带 BTC.D 的实际 runner 输出直接调用 C1 `validate_result_v4` 都返回 `None`。
但同一完整合法 34 位本金 payload，在默认 Decimal 精度28上下文返回 `'result.v4 invalid arithmetic or shape'`，在声明的 `decimal128(exact=True)` 上下文返回 `None`。
定位 C1 `portfolio_result_v4.py:37-42` 的 `decimal_string` 调用 `canonical_decimal_str`，后者默认上下文 `normalize()` 会截断合法的29–34位数字；校验数字的阶段在本函数的 exact 核账上下文之外。
`check_server.py` 保留完整可重复证据。D 按34位契约实现、拒绝有损计算；C1需由 worker 补齐校验入口上下文。本批不修改 TokenBeep，未宣称所有34位输出在当前C1默认上下文可被接收。
