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

Claude Sonnet5.5独立只读一轮完成，CLI EXIT=0，is_error=false，modelUsage仅claude-sonnet-5-5；只做静态审查。全量将在返修验证后收尾跑一次。


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
