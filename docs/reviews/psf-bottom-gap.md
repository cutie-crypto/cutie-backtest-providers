# PSF-BOTTOM-GAP 独立审查与验收

- 基线：origin/impl/psf-p2 @a440830；独立分支 impl/psf-bottom-gap。
- 异模型：Claude Sonnet 5.5，一轮，CLI `claude-sonnet-5-5`，EXIT=0。
- 审查方式：只读 Read/Grep/Glob，未运行测试或 git diff；只读四份源文件。下列测试证据由外援本地执行。

## Sonnet 原始结论

**结论:代码未发现阻断或高严重度问题;新增测试有一处中等覆盖缺口。**
限制:本会话 Bash 被禁用,我没法跑 `git diff a440830`,也没法跑测试。我读了 4 个文件的当前内容。"只去掉 queue 限制"这点只能从现状推断,没法对比 diff 确认,2 passed / 2 failed 也是采信你给的数字。

1. **目标 guard,无问题。** `strategy_bottom_patterns.py:113` 和 `strategy_top_patterns.py:113` 用 `self._broker._adjusted_price(order.size, opening)` 作参考价。多头 `>=`、空头 `<=`,等于目标也跳过,没有用信号收盘价。这个判断不再受 `_pattern_confirm_queue` 限制,普通路径和确认路径都生效。
2. **stop 优先与上报,无问题。** stop 分支(`:107`)仍然先判断,用的是原始 open,和之前一样。target 分支是 `elif`,两个分支都累加 `skipped_entry_count` 并追加 `skipped_entries`。多头时 stop < neck < target,两个条件不会同时成立。
3. **sizing 顺序,无问题。** guard 在 `init` 里 `_risk_init()` 之后才包装 `_process_orders`(`:91`、`:101`),而 sizing 在 `cutie_backtesting_provider.py:3304` 的 `_risk_init` 里安装。所以 guard 在外层,先 `order.cancel()` 再进 `sized_process`。被跳过的单不会产生 sizing 的 fills 或 rejections,README 里"在 position sizing 之前"成立。`_adjusted_price` 只用 `order.size` 的符号,sizing 的 `_replace(size=...)` 发生在 guard 之后,不影响判断。
4. **README `README.md:129-138`,无问题。** 描述与代码一致:等于目标也拒绝,开启和关闭确认都适用,报告字段是 `skipped_entries` 和 `skipped_entry_count`。
5. **测试 `test_psf_bottom_gap.py:19-34`,中等。** 这两条用例覆盖了等于目标、越过目标、sizing 无 fills 和 rejections。但它们全程没开 pattern confirmation:既没传 `pattern_confirm_bars`,`:41` 的直接 Backtest 也用 `build({})`。"确认路径也生效"这条核心改动没有被回归覆盖。建议补一个开确认的参数化。我没查其他测试文件里是否已有对应覆盖。
6. **测试里的 slippage 假设,无问题。** `:36-47` 用 `spread=0.01` 直接跑 Backtest,断言 `adjusted_open == opening*1.01` 或 `*0.99`,没有假设 HTTP slippage 会改变引擎价格。HTTP 用例在 offset 0 / ±1 时断言 `adjusted_open ≈ opening`,也与 spread=0 一致。`:49-56` 用来防止回退成用信号收盘价,有效。
7. **测试里的次要缺口,低。** 空头 spread 只在 0.5 的间隔下测过,精确相等加 spread 的边界没测。stop 优先和 `skipped_entry_count` 累计也没有在新测试里直接断言,只靠既有用例。

full suite 我没跑,按你要求等父分支 PSF-P2b-3 更新后再跑一次。

## 外援增量处理

- 中等覆盖缺口已补：两条新增测试均覆盖 confirmation 开/关，四种形态，等值/越界；复用 existing skipped_entries/count，sizing fills/rejections 均空。
- 低等级建议：既有 stop 优先及累计计数继续由原回归验证；本单直接引擎 spread 检查调整后价格越界，零 spread 精确等值由 HTTP 用例验证。
- 新增两条原实现 2 failed / EXIT=1；修后 2 passed / EXIT=0。
- 移除两侧 target 判据：2 failed / EXIT=1；cp 还原：两侧 cmp EXIT=0，2 passed / EXIT=0。

## 入场参考价判据

- `backtesting-py/strategy_bottom_patterns.py:104`、`strategy_top_patterns.py:104` 读取实际成交根 Open；两侧 `:113` 调用 `_broker._adjusted_price(order.size, opening)`，long >= TP / short <= TP（等号拒绝），先 cancel 再调用原 process_orders。
- backtesting.py 0.6.5 `backtesting/backtesting.py:915`：market orders 且 trade_on_close=False 取本根 open；`:954` 计算 adjusted_price，`:1006` 用它 _open_trade。构建器 `_build_bottom_pattern` / `_build_top_pattern` 不设 trade_on_close，保持默认 False。
- 信号根收盘不作为参考；新用例确认收盘越界但下一根开盘在目标内仍能开仓。HTTP slippage 为另行记账；直接引擎 spread 用例才检验 adjusted fill。

## 最终收尾证据

- 相关模块定向：3953 passed / 6 failed；四个等于目标的 gap 金样、一条旧 overshoot 断言及源文件字节锁已按本单口径适配。
- 现有 `off_digest` + 实际 HTTP 在 a440830 与当前候选各重算 54 个回包；仅四个 gap 场景交易从1笔变0笔，其余50个场景完整 trades（含入场/方向/出场）相同。
- 金样只写入现有 off_digest 重算的四个值，无手改 JSON 指纹；非 gap 及其他模板金样保留。
- 变更新增确认路径后单文件：2 passed / EXIT=0。下述对照表待 impl 转统领批；PR 必须保持 draft。
- 尚未出现 `/tmp/psf-p2b3/REPORT.md`，故最终父分支接续与一次全量门暂未执行。

文件 · 用例 · 旧笔数 / 新笔数 · 被拦的入场根（0-based）
q17b_39bc346_off_sha256.json · double_bottom.gap · 1 / 0 · 54
q17b_39bc346_off_sha256.json · inverse_head_shoulders.gap · 1 / 0 · 54
q17b_39bc346_off_sha256.json · double_top.gap · 1 / 0 · 54
q17b_39bc346_off_sha256.json · head_shoulders.gap · 1 / 0 · 54


- 阶段收口定向：`cd backtesting-py; python3 -m pytest -q tests/test_psf_bottom_gap.py tests/test_q17b_patconf_guards.py tests/test_short_pat1.py` → 285 passed，EXIT=0；编译及 git diff --check EXIT=0。
- 曾从仓库根跑相同文件路径造成 Q42-C2 node-id 注册口径错位（4个其他模板红）；切回要求的 backtesting-py cwd 后以上门通过，未修改其他模板或金样。

## PSF-P2b-3 接续

- 统领 /tmp/orch-out/impl-psf-gap-ok.md 已批四个 gap 各 1→0 笔、另50场景零变化，允许全量通过后脱 draft。
- 普通 merge 接 origin/impl/psf-p2 @3121709；唯一冲突在 test_q17b_patconf_guards.py 原 overshoot 块，保留本单已批的0笔及 skipped_entries 精确断言；父线其余代码、健康门与文档保留。
