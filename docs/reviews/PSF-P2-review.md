# PSF-P2c 海龟原生组保护候选

基线 origin/impl/psf-p2 @3121709；独立 impl/psf-p2c，排PR96、PR100之后；海龟金样变化待统领批，PR保持draft。任务由tokenbeep-impl派发，详见本单回报。

## 本地证据

新增tests/test_psf_p2c_turtle_fills.py两条：旧实现2红、原生候选2绿；恢复整个旧_TurtleGroupMixin（仅该类运行时变化）变异2红/EXIT1，cp恢复cmp0、2绿/EXIT0。四个存量海龟文件+新增文件共266 passed/0 failed/EXIT0；compile/diff-check EXIT0。第一次定向19红为旧退出/优先级断言；进一步发现原候选先按整根TP处理会取消开盘加仓，已修为安全开盘先成交加仓再刷新全组保护，开盘已穿旧保护则先退出旧组。两处通道退出断言因批量替换误改已恢复，定向转绿。

## 金样自动重算与因果

重算命令：cd backtesting-py && python3 tests/fixtures/capture_turtle_time_off_4d29dfa.py --record-psf-p2c。独立hand/directional_hand重算三份expected；既有response()四case各捕获两次验证确定性。baseline_sha指原市场seed，capture_source_sha256对应海龟类源字节并由fixture测试校验。非海龟运行时代码逐字一致（剔除_TurtleGroupMixin比较）；非海龟金样零改动，证据scope-audit.json。

原生保护成交释放资金并结束旧组的时间提前，同根仍禁入，但下一根可以重新评估突破。
双向金样原来第23根被退出成交挡住；现在第22根146全平，第23根决定反向，第24根100建空（原51根91），总组数3、总笔数6不变。
long_risk_layer 的单TP在17/119.7、20/155.4当根成交（旧18/130、21/163），使后续突破能更早发单，新增22根164开组并当根止损，组数4→5。
退出价和已实现盈亏改变可用权益，故后续固定单位风险的q/数量也随之重算；不是额外信号或手改入场条件。

文件 · 用例 · 旧组数/新组数 · 入场根/价 旧→新 · 出场根/价 旧→新
turtle_long_golden.json · long · 3/3（笔6/6） · 16/114.0,18/130.0,19/148.0,21/163.0,51/109.0,101/99.0→16/114.0,18/130.0,19/148.0,21/163.0,51/109.0,101/99.0 · 23/140.0×4,54/100.0,104/100.0→22/146.0×4,54/100.0,104/100.0
turtle_short_golden.json · short · 3/3（笔6/6） · 16/86.0,18/70.0,19/52.0,21/37.0,51/91.0,101/99.0→16/86.0,18/70.0,19/52.0,21/37.0,51/91.0,101/99.0 · 23/60.0×4,54/100.0,103/101.0→22/54.0×4,54/100.0,103/101.0
turtle_both_golden.json · both · 3/3（笔6/6） · 16/114.0,18/130.0,19/148.0,21/163.0,51/91.0,101/99.0→16/114.0,18/130.0,19/148.0,21/163.0,24/100.0,101/99.0 · 23/140.0×4,54/100.0,103/101.0→22/146.0×4,54/100.0,103/101.0
turtle_time_off_4d29dfa.json · long · 3/3（笔6/6） · 16/114,18/130,19/148,21/163,51/109,101/99→16/114,18/130,19/148,21/163,51/109,101/99 · 23/140×4,54/100,104/100→22/146×4,54/100,104/100
turtle_time_off_4d29dfa.json · short · 3/3（笔6/6） · 16/86,18/70,19/52,21/37,51/91,101/99→16/86,18/70,19/52,21/37,51/91,101/99 · 23/60×4,54/100,103/101→22/54×4,54/100,103/101
turtle_time_off_4d29dfa.json · both · 3/3（笔6/6） · 16/114,18/130,19/148,21/163,51/91,101/99→16/114,18/130,19/148,21/163,24/100,101/99 · 23/140×4,54/100,103/101→22/146×4,54/100,103/101
turtle_time_off_4d29dfa.json · long_risk_layer · 4/5（笔4/5） · 16/114,20/150,51/109,101/99→16/114,19/148,22/164,51/109,101/99 · 18/130,21/163,54/100,104/100→17/119.7,20/155.4,22/147.421875,54/100,104/100

## 异模型审查

Claude Sonnet5.5独立只读一轮完成，CLI EXIT=0，is_error=false，modelUsage仅claude-sonnet-5-5；只做静态审查。Closing full run completed after incremental verification; see the final gate below.


### Sonnet original result

**结论**:静态审查未发现阻断级缺陷。我没跑测试,也没写文件,不对测试结果下结论。已知"新增 2 条旧实现/变异均红"和 focused-green.log 我都没复验。有 1 个 Medium 和 3 个 Low,都不是运行时错误。

- **核查 1(触价当根/跳空/全 unit 同止损)**:通过。`provider:4424-4434` 中 SL 用 `insert(0)` 排在所有 TP 前。多头 SL 成交价取 `min(open, stop)`,跳空开盘成交。
- **核查 2(错序/漏保护/保证金拒单)**:通过,逻辑上未发现漏洞(`provider:4436-4458`)。
  - 开盘跳空穿旧止损或旧止盈时,不撤保护,旧组先结束,并由 `on_reduce` 撤掉待加仓。
  - 其余情况先撤保护,加仓在开盘成交,再按新止损/TP 重设全组保护,同根再处理一次。
  - 保证金拒单走 `_turtle_sync` 的未成交分支,随后 `protect()` 以原止损恢复保护,没有漏保护。
- **核查 3(只禁退出根)**:通过。`provider:4504` 只用 `_turtle_closed_bar` 拦住退出根,下一根可接反向。新用例 2 断言反向在 bar24 入场、bar23 无入场。
- **核查 4(优先级/标签)**:通过。原生 SL/TP 在 broker 阶段先成交,`next` 里持有期/通道在其后。末根 `protect()` 清掉 SL/TP,仍走 end_of_data。普通 close 不满足 `order in (_sl_order,_tp_order)`,不会误标。
- **核查 6(范围)**:通过。`provider` diff 的三处 hunk 都在 `_TurtleGroupMixin` 内,其余只有 README 和海龟测试/金样。

**发现**
1. **Medium** `provider:4440-4451`、`test_turtle_template.py` 里 `hand`/`directional_hand` 的新增止损段、`test_psf_p2c_turtle_fills.py:11-34`:"开盘跳空穿旧保护且同根有待加仓"这条分支没有任何用例。新 gap 用例落在 bar22,此时 4 单位已满,没有待加仓。`hand` 是先填加仓、再按新 `last` 算止损,与实现的"旧组先结束"在这个边界上不一致。删掉 `stop_gap`/`take_gap` 判断的变异测不出来。
2. **Low** `provider:4432/4434`:backtesting 的 `Trade.sl/tp` setter 断言 `0<price`。多头 `fill−mult·N≤0`(高 N、低价)时,旧版只是永不触发,现在会抛 AssertionError。
3. **Low** `capture_turtle_time_off_4d29dfa.py:78`、`README:16`、`test_turtle_time_layer.py:61-72`:`capture_source_sha256` 是整份 provider 文件的哈希,没有任何测试读它,"钉真实实现"实际只是元数据。任何非海龟改动都会让它过期。同处"byte-identical to 4d29dfa"的注释已过时,实际比的是 P2c 重录结果。
4. **Low** GOLDEN-TABLE 第 8 行 `long_risk_layer`:该金样是 provider 自录,没有独立手算。我只验了 17/119.7=114×1.05 和 20/155.4=148×1.05 吻合,22/147.421875(入场同根止损)未独立验证。原生 TP 跳空也没有单测。

### 增量处置与亲读

- Medium：新增第1条回归补多/空旧SL及TP开盘跳空且有待加仓边界；独立hand/directional_hand先按旧保护开盘退出并取消加仓，逐笔对照引擎。
- Low 非正SL：正价格市场中该止损价永不可达，保留数学组止损元数据但不设置非法broker SL；独立N=28、stop=104-5x28=-36用例验证持仓保留且sl=None。
- Low 哈希及注释：仅哈希海龟类，既有fixture测试校验当前类源，非海龟变更不导致过期；off态注释改为PSF-P2c回包与原市场seed。
- Low 手算及TP跳空：纯OHLC Wilder递推独立算N，验证22根stop=147.421875及同根成交；多/空TP跳空已补入第1条用例。
- 增量定向266 passed/0 failed/EXIT0；未再开第二轮异模型，按常备令只亲读返修增量，生产改动仍限海龟类。一次review落档命令stdin编码失败，改ASCII传入Unicode JSON后补齐；该失败不算review完成证据。


## Final local gate

- backtesting-py cwd: python3 -m pytest -q tests, sole closing full run: 10683 passed / 0 failed, 165 warnings, 845.69s, EXIT=0. Logs: /tmp/psf-p2c/full.log and full.exit. Baseline 3121709 parent report: 10681 passed / EXIT=0; introduced failures=0.
- Final old next-open-exit mutation: both added tests failed / EXIT=1; cp restore cmp=0, both passed / EXIT=0. Expanded boundary assertions are inside the same two new tests.
- Post-review replay matches the entire prior approval table and all captured payloads (post-review.json vs final.json). Scalar seeds and HTTP responses were generated, never hand-edited. Source beyond the Turtle mixin and non-Turtle goldens remain identical to 3121709.
- Sonnet 5.5: one independent static round, 1 Medium and 3 Low resolved; increment read locally, no second review round. Compile and diff-check EXIT=0. No main merge / provider pin / production call. PR101 remains draft until commander approves the 7-case table, ordered after PR96/PR100.

- Original task archived at docs/reviews/PSF-P2/tasks/PSF-P2c.md under the standing orders. The Owner follow-up reaffirmed: first close BOTTOM-GAP on 3121709, then return to this branch created above 3121709; this ordering was followed.


## Approved close: main base and fixture causality

- Commander approved all 7 Turtle golden cases; conditional close requires both group replacement chain and bar22 OHLC/stop proof in fixtures/README.md.
- Main@0e8b0ce contains PR96 and PR100; ordinary merge into this pushed branch had no conflicts. Relative to this main, non-Turtle runtime and goldens are unchanged. Relative to eea9b81, all Turtle runtime bytes and 4 JSON goldens are identical.
- Raw bar22 O/H/L/C=164/166/145/155; signal21 Wilder N=8.2890625, stop=164-2*N=147.421875, 145<=stop<164<=166. Existing test additionally verifies raw OHLC, entry164, stop exit147.421875 and negative PnL; both remains bar22 full stop -> bar23 reverse -> bar24 open100 -> old bar51 group replaced.
- Incremental focused tests: 2 passed / EXIT=0; compile and diff-check EXIT=0. Evidence: /tmp/psf-p2c/approved-close/proof.json, focused.log, review.diff.

### Sonnet 5.5 incremental result (static only)

**结论：静态审查未发现明确阻断问题。两点机理都与我读到的 raw 和金样一致，新增断言也实际检验了 entry 164、exit 147.421875 和亏损。** 我没跑测试、没改文件。`focused.log` 的 passed/EXIT0 我没亲验，不算我的结果。

1. **both 组替换链条：一致。**
   - turtle-1 四个 unit 的入场价是 114/130/148/163，全部在第 22 根按 146.0 平仓（`turtle_both_golden.json:583-614`）。
   - 146 等于 163 − 2×8.5，与 README 的“加仓后共享止损更新为 146”对得上。
   - 第 22 根 low=145（`:309`）触及 146，第 23 根 open=140，第 24 根 open=100.0（`:27`），第 51 根 open=91（`:54`）。
   - turtle-2 是 signal=23，第 24 根以 100.0 建空，第 54 根平仓（`:616-621`、`:643-651`）。原 51 那组不在 expected 里。
   - 总计 3 组（turtle-1/2/3）、6 笔，`units_skipped` 为 1。
   - 对照同夹具 long 金样：第 51 根入场价 109 的 turtle-2 是单独一组，both 里已被 24→54 的空组取代，说法属实。
2. **long 第 22 根真实同根止损：成立。**
   - long 金样第 22 根 O/H/L/C 是 164/166/145/155（`turtle_long_golden.json:25`、`:167`、`:309`、`:451`）。
   - 164 − 147.421875 = 16.578125 = 2×8.2890625，算术自洽。
   - 145 < 147.421875 < 164 < 166，所以开盘没穿止损，当根低点才触及，不是原价幽灵单。
   - 新增断言：`test_psf_p2c_turtle_fills.py:93-94` 锁 OHLC 和止损区间，`:100` 断言 entry=164，`:101` 断言 exit=stop，`:102` 断言 PnL<0。`:99` 的列表相等保证 `:102` 不会因为空集合而空过。

**Findings（均为 Low，不阻断）：**
- **Low，`test_psf_p2c_turtle_fills.py:115-118`：** README 声称“原 51 不再出现、3 组/6 笔”，但该测试只断言 turtle-2 的 `EntryBar==[24]` 以及 `23 not in EntryBar`。它没有直接断言第 22、51 根无入场，也没断言总组数。这部分只靠 JSON 金样兜底，可补一句 `51 not in set(trades.EntryBar)`。
- **Low，README 第 15 行：** “4→5 组”和 long_risk_layer 的 HTTP 金样在 `turtle_time_off_4d29dfa.json` 的超长行里，我没核对。上面的 164 和 147.421875 是引擎层（无 fee/slippage）的值，HTTP 金样有 10/5bps，成交价可能不同。README 没说明这一点。
- **未核对：** N=8.2890625 的 Wilder 递推我只核对了算术自洽，没逐根复算。

### Incremental Low findings disposition

- Low group-count / bar51 coverage: the focused reverse test is supplemented by existing test_directional_engine_golden_and_unique_position_direction and test_directional_scalar_golden. They compare total trade count, every entry/exit bar/price and group mapping against the independent scalar fixture; an added bar51 entry or group violates those checks. No redundant assertion-only test was added.
- Low engine-vs-HTTP distinction: decoded the existing long_risk_layer HTTP golden explicitly, confirmed 5 groups / 5 trades, turtle-3 entry164, exit147.421875, bar22 same-time trade and negative PnL. fees/slippage are separately recorded; the README now distinguishes both checks. Evidence: approved-close/proof.json.
- The review did not independently recompute Wilder N; the existing test uses only raw OHLC and arithmetic to compute N and the stop before checking the engine. This is local test evidence, not a claim that the reviewer ran it.

### 1013 追加条件核验

ARM-C0-DS 先完成后续接。逐份读取三份自动重算金样的原始第 22 根及 turtle-1 退出价：long/both OHLC=164/166/145/155、SL=146；short=36/55/34/45、SL=54；四个 unit 都在 22 根退出，各止损都落在当根区间内，无阻断条件。README 增补逐份价格表，以及单 TP 当根实现盈亏影响可用权益与后续风险数量重算的机制。运行时代码及金样未再改；这是前述 Sonnet 5.5 review 后的文档增量，按常备令亲读核对，不增加审查轮。最终按任务指定 no:cacheprovider 命令全量，在 REPORT 中登记真实结果。
