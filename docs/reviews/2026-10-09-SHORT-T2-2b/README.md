# SHORT-T2-2b：逐仓仲裁、引擎权益与公开期货门

基线 `e25886e`，指定分支 `feat/1009-cx1-t2-2b`，未切分支、未碰 main。
这是本地源码与合成行情证据；HTTP 是 FastAPI TestClient 进程内请求，服务端复核是 T2-2a 仿真。
未做真实 provider 行情、服务端接收、部署、UI、设备或生产验证。

## 行为和权威文档

当前行为文档：根 `README.md` 的 Single-position template leverage 节已同步。

- MMR=0；爆仓价由 `_isolated_liquidation_price` 用 E*(L−1)/L 或 E*(L+1)/L 求出，只除一次。
  T2-2a 所有原有数值手算用例不变；该组 44 条全绿，其中仅最后一条门断言按 worker 定更新。
- 共享候选函数 `backtesting-py/cutie_backtesting_provider.py:2515`；风控接线 `:2724`，旧路径接线 `:2793`。
  风控层跳空优先；非跳空时只让更近的有效止损先触，等价位爆仓。
  旧层是收盘口径，任何盘中爆仓都先于其止损/止盈/到期。模板信号也让位。
- 两键爆仓记录不变；有在途平仓单即返回，防重复；当根两方向开仓都被阻止，次根决策允许再开。
- `_isolated_install_settlement` 位于 `:2607`：每个杠杆请求包住自身 broker 的 `_close_trade`，
  原平仓后按 result.v2 费用、滑点、改写成交的累计 pnl 重设现金；在继续处理订单与归零检查前完成。
  使用用户资金的 Decimal scale 计算，最后才折回引擎 float，不改第三方库文件。
- 补充边界：broker 先于 Strategy.next 检查归零。50% 保证金、5 倍、E=100、qty=25、当根 Close=50
  会在仲裁前造成库浮亏 −1250。订单钩子复用同一仲裁点，若逐仓结算仍有正余额就桥接待结算现金；
  最终损失 −500、余额 500、下一笔 qty=12。真实耗尽仍走库的归零检查。
- 3b 部分止盈与最后爆仓共享 opened_at；运行时另存剩余整数 units，在改写和报告时只选最后剩余批次。
  两键记录及 result.v2 冻结键集不变，2a 对缺少运行时身份的重复 opened_at 仍拒绝。
  手算：先平 5 单位 pnl=0，剩余 5 单位在 80 爆仓 pnl=−100，余额 900，报告 margin_lost=100。
- run_backtest 只为 futures L>1 传记录及运行时剩余 units，合入 isolated_risk / isolated_margin；
  spot L>1 取数前拒绝，账本拒绝 leverage、组合 [1,3] 不动。
- stop_loss_pct 来自原始请求百分数。动态计数仅统计启用 ATR/移动/保本且开仓初始止损已在爆仓价之外
  （含相等）的笔数，不统计之后的移动。固定距离通过原始百分数独立判定；定义写入 assumptions。

## worker 定与 codex 定

- worker 定：第 3 条跟随统领二次改裁，跳空引擎权益与 result.v2 同步，可超过保证金损失；
  保证金封顶只作 margin_lost / loss_beyond_margin 披露。非跳空 pnl=−保证金−fee−slip。
- worker 定：结算文件只改最后一条旧门用例并单独提交 `6e501b8`；其余文件前缀与 e25886e 字节一致。
- codex 定：现金回调同时对普通平仓计入冻结滑点，以保持杠杆路径累计权益一致；关闭态不安装回调。
- codex 定：归零检查前的现金桥接与运行时剩余单位 sidecar，修复上述引擎及部分批次边界。
- codex 定：旧参数门文件的 futures 两条用例改验“已进入取数后空行情拒绝”；spot 仍验取数前拒绝，条数不变。
- codex 定：新增关闭态检查从 Git 固定 SHA 加载源码，不改任何 golden/fixture。
  运行时枚举 22 个模板加 ema_pullback 空头分支，共 23 案例 × {缺省、显式 1、spot} = 69。

## 验证

| 门 | 原文 | EXIT |
|---|---|---|
| 开工基线 | 3587 passed, 30 warnings in 58.13s | 0 |
| 仲裁＋结算 | 140 passed, 4 warnings in 6.88s | 0 |
| 既有参数门 | 254 passed, 2 warnings in 2.08s | 0 |
| 收尾全量 | 3683 passed, 32 warnings in 62.07s (0:01:02) | 0 |
| validator | 29 passed in 0.53s | 0 |

全量算式：3587 + 96 = 3683。
新增 96 = 同根 16 + 动态 3 + 再入场权益 2 + 无止损旧层 2 + HTTP 2 + 关闭态 69 + 归零/部分批次 2。
旧门用例修改不增减条数；风险与时间兼容文件原样全绿，既有 fixture/golden 未动，validator 未改。
归零边界修复前红证据 `/tmp/t22b/insolvency-red.log`：1 failed, 94 deselected，EXIT=1。
HTTP 在注册的 EMA(2/3) 上触发两笔爆仓，完整 isolated 节点按独立手算值断言；服务端冻结公式仿真通过。

原信命令在 backtesting-py 下执行（validator 门在 ../validator 下）：

```sh
python3 -m pytest tests -q -p no:cacheprovider > /tmp/t22b/baseline.log 2>&1; echo "EXIT=$?"
python3 -m pytest tests/test_isolated_liquidation_arbitration.py tests/test_isolated_liquidation_settlement.py -q -p no:cacheprovider > /tmp/t22b/focus.log 2>&1; echo "EXIT=$?"
python3 -m pytest tests -q -p no:cacheprovider > /tmp/t22b/all.log 2>&1; echo "EXIT=$?"
python3 -m pytest tests -q -p no:cacheprovider > /tmp/t22b/validator.log 2>&1; echo "EXIT=$?"
```

## 七条变异

每条均备份 → 可编译变异 → 指定用例红 → 复制还原 → 指定用例绿 → cmp=0，不使用 git checkout。
完整失败名称见 `mutations.json`；重跑 `python3 docs/reviews/2026-10-09-SHORT-T2-2b/mutations.py`。

| 变异 | 红 | 还原绿 | cmp |
|---|---|---|---|
| 01_stop_first | 2 failed, 138 deselected, 2 warnings in 0.99s / EXIT=1 | 2 passed, 138 deselected, 2 warnings in 1.05s / EXIT=0 | 0 |
| 02_equal_stop | 2 failed, 138 deselected, 2 warnings in 1.02s / EXIT=1 | 2 passed, 138 deselected, 2 warnings in 1.10s / EXIT=0 | 0 |
| 03_no_gap | 2 failed, 138 deselected, 2 warnings in 1.12s / EXIT=1 | 2 passed, 138 deselected, 2 warnings in 0.96s / EXIT=0 | 0 |
| 04_legacy_early_return | 1 failed, 1 passed, 138 deselected, 2 warnings in 1.02s / EXIT=1 | 2 passed, 138 deselected, 2 warnings in 0.88s / EXIT=0 | 0 |
| 05_percentage_units | 1 failed, 139 deselected, 2 warnings in 1.26s / EXIT=1 | 1 passed, 139 deselected, 2 warnings in 1.21s / EXIT=0 | 0 |
| 06_no_equity_correction | 2 failed, 138 deselected, 2 warnings in 1.00s / EXIT=1 | 2 passed, 138 deselected, 2 warnings in 0.94s / EXIT=0 | 0 |
| 07_spot_open | 1 failed, 139 deselected, 2 warnings in 1.18s / EXIT=1 | 1 passed, 139 deselected, 2 warnings in 0.99s / EXIT=0 | 0 |

变异结束恢复源码 SHA256：`82b0f97612df8562ba37128f2593bcc922be6fb3cb034b63d4522c5132a6e8d5`。
之后仅更新两条源码 docstring，再过收尾全量；最终源码 SHA256：
`cdd29d04bbe574faf7eb73f211c3de32eabb1fb550bf49de21392eaebc843f84`。

尾注原命令输出 0（grep 无匹配的退出码 1 是其标准行为）：
`git log --format=%B origin/main..HEAD | grep -ciE "^(co-authored-by|claude-session):"`。

## 交付边界

按主题提交，仅推指定分支，不开 PR，不合 main。服务端放行与两端 UI 属另批。
本实现依赖本机 backtesting.py 的私有 broker 钩子；库升级时须重跑上述权益与归零边界用例。
