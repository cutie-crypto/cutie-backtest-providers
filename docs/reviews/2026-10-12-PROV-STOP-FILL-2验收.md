# PROV-STOP-FILL-2 本地实施验收

基线：5834faaf85956ad941003c98c34d446c806cfbae（pin 13）。分支 fix/prov-stop-fill-2。
仅 provider 仓、本地 commit；不 push、不开 PR、不改 pin、不碰 TokenBeep。
异模型 review：由 impl 亲读 + 统领 PR 亲读，留待本地交付后执行。

## 根因与裁定

原生保护单在 broker 成交后，protected_next 跳过模板 next。macro/funding 没有补记钩子，liquidation 继承这条路径，导致当根事件没有 status。event_window 基线 :151–154 已有钩子，原本就绿；impl 裁定修前/变异均三红一绿，修后全绿。
公共 helper skip_native_exit_events 只给未消费事件写 skipped_in_position。event_window 保持原输出；macro/funding 的共用钩子覆盖 _by_event、_by_entry 和 _watching，推进 _next_bar；liquidation 只扩展 _liq_by_bar 并维护 signal_counts。原生保护单出场当根的禁止再入场时钟、价格与下单路径不变。
复用 skipped_in_position：该根已被持仓退出占用，不能再入场；macro 系补 reason=native_exit_no_reentry。不增加状态，strategy_event_window 的无 status 报错分支无改动。

## 全入口核对

- event_window：_build_event_window → make_event_window_strategy → assumptions.event_window 对账；已有钩子上提公共 helper。
- macro_release_breakout / macro_surprise_direction / fomc_reversal：_build_macro_event → make_macro_strategy → raw_report.<kind> 对账。H2 代表共同公布队列；H3 另加真实延迟入场回归。H1 没有通用百分比 SL/TP，_risk_native_enabled 为 false，原区间风险路径及持仓重叠守卫不变。
- funding_settlement_reversal：_build_funding_reversal → make_macro_strategy，同一补记钩子。用允许的行情缺口使上一持仓跨到下一结算决策根（最长持有参数仍为 240 分钟）。
- liquidation_reversal：_build_liquidation_reversal → make_liquidation_strategy(make_macro_strategy)；日值队列扩展共用枚举器，补记后照常计数。
- token_unlock_event：strategy_token_unlock 自有 _actions 钩子在基线已标 skipped_in_position，并已有 native_decisions；本单不改。唯一另一 settle_event_window_exits 调用为其 settle_report。
- _native_decisions 的入口只有 event_window / token_unlock；macro 原有出场原因对账不在本次 status 修补中重写。

## 验证证据

系统 python3：backtesting 0.6.5 / pandas 2.3.3。
四类用例使用真实 provider HTTP route / 本地合成行情 / mock 中心指标，不调用生产。结算捕获真实 ValueError。
每次红/绿均锁每笔入场决策根时间、opened_at、entry_price 和笔数；第二个事件不入场，第三个事件照常入场。
所有金样文件保持原字节，不更新任何金样；完整既有 golden 门由全量 pytest 覆盖。
还原证据：cp 备份三份修复源码 → git show 5834faa 逐文件覆盖 → 四类定向：3 failed, 1 passed，EXIT=1；三条同一 missing-status ValueError → cp 恢复且逐字节比对 PASS。
变异证据：只去掉 macro 共用钩子的补记调用 → 四类定向：3 failed, 1 passed，EXIT=1；三条同一 ValueError → cp 恢复且逐字节比对 PASS。
日志：/tmp/psf2/baseline-red.log、mutation-red.log、red-proof.log、t.log、paths.log。
恢复后专项：7 passed, 2 warnings，EXIT=0（4 条主回归 + H1/H3 路径及公共 helper 不覆盖既有状态守卫）。
全量收尾：10591 passed, 185 warnings in 240.49s (0:04:00)；EXIT=0，相比 PR #90 的 10584 passed 增加 7 条，无 failed/skip/xfail。
静态对比：skip/skipif/xfail 匹配基线 0、当前 0；next 与 settle_event_window_exits AST 完全一致；git diff --check EXIT=0。
pre-commit 尝试 python3 -m pre_commit run --all-files：No module named pre_commit，EXIT=1。provider 仓没有配置、core.hooksPath 或有效 Git 钩子，仅 pre-commit.sample；该门未通过，已向 impl 请求具体命令/缺门裁定。
本记录不代表生产 pin 14 或真实交易验证。
