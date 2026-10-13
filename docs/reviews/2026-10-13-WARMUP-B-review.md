# WARMUP-B 异模型审查与证据状态

实现：GPT Codex；独立审查：Claude Sonnet 5.5 (`claude-sonnet-5-5`)，CLI 实际 modelUsage 已核。只读、未运行代码。

状态：部分交付，尚不具备合并或换 pin 条件；旧 HTTP 预热模拟与完整响应金样适配未完成。main 全量基线：10644 passed / 1 failed（CPU backtest 健康响应 <100ms 门失败）。

NONE 不成立,我有 5 条发现。以下只依据所给的 diff 和契约。我没有运行任何代码,也看不到 `warmup_b_demo_v1.json` 的内容。diff 没带行号,所以位置用函数名标注。

1. **冻结复放没有接入运行路径(阻断)**
   - 位置:`cutie_backtesting_provider.py` 的 `_load_warmup_snapshot` 和两处运行入口(`_run_scale_in_out_backtest`、`_run_backtest_sync`)。
   - 问题:`_load_warmup_snapshot` 只在测试里被调用。两个运行入口每次都用 `_fetch_template_warmup` 现取的 `warm`,从不按 `snapshot_ref` 读冻结行喂给引擎。
   - 失败场景:中心行情在 run 之后修订了播种段的某根 K 线。拿同一个 `snapshot_ref` 重放,没有任何入口可以传入这个引用,结果只能是新的 manifest 和新的逐笔,与原 run 不一致。
   - 现有测试 `test_warmup_exact_history_repeat_is_identical` 靠 mock 数据不变才相同。它里面「changed」那段恰好证明了现取行为会漂移。
   - 契约要求 B 负责「保存/复放」。目前只有保存和校验,没有复放。

2. **风控 ATR 的预热预算不是递归预算(高)**
   - 位置:`_run_backtest_sync` 里的 `risk_warmup = int(risk.get("risk_atr_period", 0))`。
   - 问题:风控 ATR 是 Wilder 递归,却按原始周期取数,没有用 `wilder_bars`。
   - 失败场景:`risk_atr_period=14` 只要 14 根,而同一递归在模板里要求约 270 根。风控 ATR 的止损距离在主区间开头仍带着未收敛的种子残差,同样的输入就能通过严格预热闸门。
   - `test_budget_separates_minimum_and_recursion` 没覆盖风控层,所以测不出这个问题。

3. **VWAP 模板的建议起点不可跑(高)**
   - 位置:`_warmup_shortage` 的 `hint`,以及 `vwap_warmup` 的计算。
   - 问题:`_warmup_shortage` 假定 R 不随起点变化。但 `vwap_warmup` 等于「首根距 UTC 当日零点的根数」,起点后移 d 根,R 就同步增加 d(同日内)。
   - 失败场景:起点后移 d 根会让实得根数增加 d,同时让 R 增加 d,缺口 `missing_bars` 不变。建议起点在跨过零点前永远不可跑。
   - 现有「建议可跑」的测试只覆盖 `ema_cross`,没有覆盖 VWAP 和其他起点相关的预算项。

4. **高周期过滤的 manifest 对齐方式忽略网格偏移(中,依赖 `_timeframe_grid_offset_ms` 对哪些周期非零)**
   - 位置:`_run_backtest_sync` 里调用 `_warmup_evidence(..., context.report['warmup_end_at'], ...)`。
   - 问题:`_warmup_evidence` 用 `ceil(start_at/step)*step` 按 epoch 对齐。高周期上下文构造时用的是 `grid_offset_ms`,也就是 `latest_opens` 带偏移的网格。
   - 失败场景:如果有 `filter_timeframe`(例如 1w)的网格偏移非零,`end_at` 会落在错误的网格上。`_kline_rows_to_canonical(warm, start, end-1)` 会裁掉或错位行。`actual_bars` 仍写成 `len(warm)`,但 checksum 对应的是另一批行。
   - 这会导致 Server 重建不一致,`_load_warmup_snapshot` 的覆盖校验也会失败。
   - 测试只覆盖了 `2h`,偏移为 0。

5. **错误分类过宽,且首根主区间缺口被误报(中)**
   - 位置:两处调用点的 `except Exception`,以及 `_warmup_shortage` 以 `main.index[0]` 为锚点。
   - 吞错:`_warmup_evidence` 在同一个 try 内写快照文件。磁盘写失败(`OSError`)或代码缺陷会变成 `WARMUP_FETCH_FAILED`,提示「历史读取失败,请重试」,与事实不符。`tempfile` 用了 `delete=False`,写失败时临时文件会残留。
   - 高周期:`HigherTimeframeContext.build` 的一次取数同时覆盖主区间和预热段,任何异常(包括纯主区间失败)都被标成 `WARMUP_FETCH_FAILED`。该处的 `NO_DATA` 也被标成取数失败,与主周期把 `NO_DATA` 当「历史不足」的口径不一致。
   - 误报:`_warmup_shortage` 以 `main.index[0]` 为锚点,而取数止于对齐后的起点。主区间首根缺失但在容差内时,实得根数被算成 0,报 `WARMUP_INSUFFICIENT`,并建议改起点。用户照做也无济于事,因为问题是主区间缺口,不是起点前历史。

**未验证的部分**
- 你们提到的 421 个旧 HTTP fixture 用例,我无法判断哪些是测试适配遗漏、哪些是真回归。
- `test_old_short_snapshot_is_rejected_by_new_revision` 替换了 `test_routed_result_v2_matches_frozen_bytes`,所以成功路径上不再有 result.v2 冻结字节的对照。
- `template_required_warmup` 对 `macd_*`、`rsi_*` 变体的参数键取默认值。只有 `macd` 和 `macd_above_zero` 带默认参数被测过,自定义参数是否会落回默认值未验证。
- 它用 `built` 里是否有 `range_config` 等键来判断返回 0,而旧代码用的是运行函数里的局部变量。`built` 是否真的含这些键,diff 里看不出来。
- 这 13 个 golden demo 的内容、Server 侧的 hash 重建、`assumptions_json` 透传,都不在所给材料内。

## 作者处置

1. 冻结复放：已新增可选 `backtest.warmup_manifest` / hash 输入，校验14键、版本/revision/身份/连续覆盖及快照内容；同主区间数据下，修改当前预热源后仍按旧快照执行逐笔与hash一致。高周期独立快照的请求复放及平台请求接线尚待做，不能宣称所有过滤复放已完成。
2. 风控 ATR：改用实际 alpha 的 Wilder 收敛预算，新增 R−1 直接用例；没有 ATR 的纯百分比止损不增加此预算。
3. VWAP：起点相关预算尚未有跨午夜建议求解器，保守不给 X；有效缺根数仍上报，新增无未证明起点用例。
4. 周线网格：已统一使用 provider 的周一 offset=345600；周线精确起止、内容及复放校验用例覆盖。
5. 分类：写旁证失败用安全 ENGINE_ERROR，临时文件 finally 清理；主区间首根缺失用 TIME_DATA_GAP 且无N/X；高周期历史 NO_DATA 经原请求包装变空并按短历史分类。一次高周期请求同时覆盖主/预热的读取失败仍按历史读取失败。

## 本地证据边界

完整历史示范13组重算，逐笔/交易数变化0组；Wilder/MACD/Keltner/volume的播种跨度及旁证有变化，确定性输入下示范交易未跨阈值。不承诺其它历史交易不变。历史短快照作为新版本输入必须拒跑，旧run应按原revision重放。

没有部署、pin、Pre、真实行情回源或设备证据。详细命令/退出码/全量逐条失败归因及剩余项由 `/tmp/warmup-b/REPORT.md` 和日志交付。


## WARMUP-B-2 增量审查

接管 codex-2 在途补丁和证据。Claude Sonnet 5.5 一轮有效review，实际 modelUsage=claude-sonnet-5-5，只有 Read/Grep/Glob；前次禁工具仅返回读取计划，未算作结论。

简要说明:我已读完生产代码 `_resolve_template_seed` 和 `_fetch_template_warmup`、新 helper、event0 与 warmup 测试,以及 conftest 的 legacy 固件。下面是结论。

# WARMUP-B-2 增量只读 review 结论

**范围声明:** 我没有运行任何测试。我没有看到全部 JSON 金样,包括 `7p3b2_off`、`patconf1`、`event0_window_golden`、`event0_window_scenarios_golden` 的内容,也没有看到 `capture-sizing-d` 脚本和 `q42c_default_path_cells.json`。以下只基于上述源码、你贴的 diff 和审计 JSON。

## Finding

**1. 中 — 金样重算只校验入场身份,出场、数量、盈亏的变化会被整文件覆盖吞掉**
- 位置:`capture-sizing{,-b,-c}.py`、`capture-pliq.py`,以及 `test_10b2c_sizing_kline_bottom.py::test_omitted_sizing_keys_match_5d0f9ac_bytes`。
- 失败条件:`identity` 只比 `(opened_at, entry_price, side)`。恒定种子会改变 ATR、EMA、RSI 等指标。如果这些指标影响了止损或止盈价、出场时点、仓位或统计,入场不变但出场、数量、盈亏会变。脚本照样 `path.write_text(new)`,变化被静默写进金样。
- 同一问题还有两处:
  - 该测试删掉了「只允许已知的 T2-2b 措辞增量」的 `replace` 断言,「与 5d0f9ac 字节一致」的约束变成了自我重录。
  - 测试名仍叫 `match_5d0f9ac_bytes`,但文件已是重录版。
- 建议:覆盖前对 old 和 new 做结构化 diff,只放行 `assumptions.warmup_*`、`indicator_warmup_bars`、`data_manifest` 和说明文本。其余字段(含 trades 全字段、`equity_curve`、`metrics`)必须相等,否则按停止文件处理。

**2. 中 — `_10b2d_cases.post` 对 `red_streak_rsi` 单独保留空种子桩,该路径应当必然失败**
- 位置:`_10b2d_cases.py:68`。
- 失败条件:`red_streak_rsi` 默认 RSI14,Wilder 预算应大于 0。我没有实际核实具体数值。空桩绕过 `_fetch_template_warmup`,`_warmup_evidence` 在 `len(warm) != required` 时抛 `WARMUP_INSUFFICIENT`。结果是 `test_10b2d_sizing_range_calendar.py` 中三处会以失败体收场:
  - `:36-38` 的 `result_status == 'success'`(golden 参数化,含 `red_streak_rsi.default` 和 `red_streak_rsi.user_stop`)。
  - `:68` 的 `red_streak_default` 用例。
  - `:70` 的 `red_streak_user_stop` 用例。
- 另外,`:141-145` 的 `test_red_streak_pricing_key_without_stop_rejected_at_validation` 期望 `INVALID_PARAMS`,这个校验发生在取预热之前,大概率不受影响。
- 问题:这是把「停止文件」的决定藏进共享 helper,表现为意外失败而非显式 `xfail`。pliq 系列对同一模板用了 `complete_seed`(审计里 lev1/red_streak_rsi 全部入场不变),两边口径不一致。
- 建议:至少加注释和 `xfail(strict)` 标注停止原因,并在交付说明里列出这批保留失败。

**3. 低 — 零预算冻结复放测试不能证明「冻结播种先于免读取捷径」**
- 位置:`test_warmup_explicit.py::test_zero_budget_frozen_seed_replays_before_history_shortcut`,运行分支在 `cutie_backtesting_provider.py:1370-1376`。
- 失败条件:有人把 `if required == 0` 挪到 `_snapshot_seed` 之前,冻结 manifest 被完全忽略。`required==0` 时两条路径都得到空种子,结果一致,测试仍绿。篡改 manifest 或 hash 应当被拒绝,这个行为也未覆盖。
- 建议:传入 hash 不匹配或字段被篡改的 manifest,断言 `INVALID_PARAMS`。

**4. 低 — 运行时增量与 `_fetch_template_warmup` 内部的 `bars <= 0` 早返回重复**
- 位置:`cutie_backtesting_provider.py:1374-1376` 与 `:1239-1240`。
- 真实读取函数在 `bars<=0` 时已经返回空 DataFrame,不读历史,也不做 `minimum` 检查。所以新分支对生产读取行为没有增量。
- 唯一可观察差别:返回值从「仅 `_WARMUP_COLUMNS`、无 attrs」变成 `main.iloc[:0]`,带 main 的 index 类型和 attrs。它的实际作用是绕开测试里会 `raise AssertionError` 的桩。
- 影响:`minimum` 校验是否被绕过,两条路径等价,不构成回归。这是先前评审里的一个疑点,现已排除。代码注释「do not call a history reader」只对桩成立。
- 建议:确认下游不依赖旧空帧的 attrs。可以保留此分支,但注释应如实说明。

**5. 低 — `complete_seed` 是常数种子,且绕过真实连续性校验**
- 位置:`_complete_seed_fixture.py::complete_seed`。
- 种子是 `main.iloc[0]` 整行复制 `bars` 次。HTTP 成功用例因此不再覆盖 `_warmup_shortage` 的连续性、同源、有限值检查。
- 好在 `test_template_indicator_warmup.py` 和 event0 的 `strict_warmup` 路径覆盖了真实读取。
- `complete_prefix` 在已看到的范围内没有调用方,可能是死代码。
- 建议:删除或标注,并在 helper 文档里写明「常数种子只用于入场/出场重录」。

**6. 低 — 捕获脚本无条件打补丁,而 conftest 对 q42c 默认路径单元格豁免**
- 位置:capture 脚本对 `_parse_fixed_risk_params` 的无条件包装,对比 `conftest.py:29-33`。
- 失败条件:若 vwap、calendar 等默认单元格出现在 `q42c_default_path_cells.json`,测试时不带补丁而金样带补丁录制,会字节不一致。
- 我没看到该 JSON,请确认 `golden_10b2a/vwap_reversion.*` 不在豁免名单里。

**已核对、不成立或无需处理的点:**
- 先前评审曾指出 `test_filter_on_gapped_warmup_fails_before_backtest` 数量与缺口混在一起,但该测试已改为 `prefix(data,21,drop=10)` 加 `strict_warmup=True`。filter 预算 20(`filter_ema_period=2`,10×2)已满足,断言 `actual_bars == 10` 且 `missing_bars == 10`,确实只验连续性。该指摘不成立。
- `test_zero_budget_does_not_read_history` 靠 `event.post` 里 `warmup=None` 时桩会 `raise AssertionError`,删掉分支会使结果不为 success,能拦住回归。该指摘基本不成立。不过它没断言 `calls == []`,建议补上。
- `test_warmup_since_is_no_later_than...` 已改为按区间过滤调用,不再依赖顺序。

## 无法确认的部分

`7p3b2_off_c72c4a1.json`、`patconf1_5ddd8eb.json`、`event0_window_golden.json`、`event0_window_scenarios_golden.json` 没有对应的审计 JSON 或捕获脚本(`capture-sizing-d` 脚本我也没看到)。`GOLDEN_SHA256` 只是新钉值,不证明 trade 字段未变。

建议另附这几个文件相对 `main` 的 trades 字段逐项比对:入场时间、价格、笔数、方向,以及出场和数量。比对结果请你们自己核对。

### 作者核验与返修

- 中1：51个变动金样、235组完整响应相对最新main结构化核对，trades全字段、equity_curve、metrics、data_manifest变化均为0，不只比较入场身份。旁证/说明变化另列，不手改JSON，沿用post/snapshot/canonical捕获。2c移除旧措辞拼接是因为新捕获已包含该措辞。
- 中2：2d/red_streak_rsi旧冷桩显式注释尚未适配；整文件27绿4红保留，不xfail掩盖失败。
- 低3：补空种子hash篡改必拒断言；把免读取捷径挪到快照校验前时指名红，cp还原22绿。
- 低4：零预算分支不调用历史入口并保持空帧属性；原读取器也免实际取数，不声称节省生产网络调用。11203行主窗口minimum检查未动。
- 低5：helper注明仅用于声明的常数成功fixture；真实读取的连续性/同源/有限性由严格用例覆盖；未使用complete_prefix已删除。
- 低6：q42c名单含vwap/calendar；本单与同命令main均从仓根跑，nodeid带backtesting-py前缀，不命中历史tests前缀名单；未改机制，其他cwd门未验。
- 停件：patconf2b1 6组、P6 10个响应入场变化，两文件自身尝试cp撤回，不改断言/金样；旧新列表见/tmp/warmup-b2/*entry-changes.json。time-layer独立63组暖态探针入场变化0，未据此改整个冷态契约。
- 高周期完整冻结复放和1M非固定网格不在本单；全量未绿则PR保持draft。

### 最终门状态

收尾一次：8682 passed / 1987 failed / 0 errors，EXIT=1；最新main同命令10646 passed，EXIT=0。故本单引入失败1987，目标未完成，PR98继续draft。停止文件及1987条候选归因均已落/tmp/warmup-b2，保持残余红而非xfail。


## WARMUP-B-3 增量只读审查（2026-10-13）

实际 modelUsage: claude-sonnet-5-5；effort=high；is_error=false；一轮，未运行测试、未写文件、未起子agent。

# WARMUP-B-3 增量只读审查

我只读了你贴的 diff 和 evidence，没有写文件、跑测试，也没有起子 agent。pin7e 的 `p6.post`、`complete_seed` 和各 importer 没法看到，相关结论标了"需核对"。

**总体判断：没有发现适配直接掩盖交易变化的证据，但有 3 处中等风险**，建议在全量前补上。

## 发现

**1. 中｜`capture_warmup_b3_disclosure.py` 的 `raw_report`/`assumptions` 整体重捕，金融指纹管不到**
- 金融 guard 只覆盖 `trades/equity_curve/metrics/data_manifest`。`raw_report` 里还有 `legacy_metrics`、`provider_summary`、`skipped_entries` 和模板诊断。这些全被新哈希覆盖后冻结。
- 触发路径：`warmup_b3_disclosure.json` 里 `macd_bullish_divergence/0` 与 `/1` 的 raw_report 哈希不同（`57b9d1…` 对 `f78900…`），`rsi_bullish_divergence`、`rsi_bearish_divergence` 同样不同。`macd_bullish_divergence` 的金融指纹是零交易（`30b5e3…`）。
- 这说明扩展预热会改变 raw_report 的信号级内容，而交易仍为零。这类变化会被重捕静默吸收，以后无法再发现。
- 建议：在 capture 里用 eligible 开/关各跑一次，对 `assumptions` 和 `raw_report` 做字段级 diff。只允许 `indicator_warmup_bars`、`warmup_manifest*` 这类预热披露键不同，否则 `assert`。

**2. 中｜capture 脚本的 `single` 段没有冻结哈希比对，也没有 audit 条目**
- `disclosure-audit.json` 只有 `filters/*` 和 `ledger/*`，没有 `single/*`。
- `single` 只受 `checked_response` 的 4 字段金融指纹约束。result.v2/engine 哈希要等以后跑测试才比对。
- 这样"原 engine/result/v2 哈希不改"对 `single` 只是事后由测试兜底，不是 capture 时的拦截。
- 建议：对 `time.BASELINE['single'][name][warm]` 做与 filters 段相同的"除三键外全等"断言，并写进 audit。

**3. 中｜共享模块 `capture_time_layer_11e8cfb.response()` 的作用域串漏**
- allowlist 命中 `(name, core_params, warm)` 就对所有 import 它的文件生效，不限于 time/entry。
- 如果 mtf、sizingd、plow、isolated 或 patconf2b1 系列也走 `capture.response('breakout', {...}, True)` 这类调用，它们会静默改用合成历史。这与"不碰其余 22/188/177"冲突。
- 你给的 mtf 135/95、sizingd 31 没有 2840fa9 对照。
- 建议：grep 该模块的 importer。逐文件对比候选与 2840fa9 的失败用例 ID 集合，不要只比计数。

**4. 中低｜allowlist 里 39/145 的金融 guard 没有区分力，绿数偏乐观**
- 这 39 个 case 的指纹都是零交易 `30b5e3…`：
  - `calendar_schedule`×6
  - `bearish_engulfing`、`shooting_star`、`evening_star`、`three_black_crows`、`macd_bearish_divergence`、`macd_bullish_divergence` 各 4
  - `fibonacci_retracement`×4
  - `cme_weekend_gap@long`×2
  - `macd_above_zero`×1
  - `volume_breakout`×2
- 它们变绿只证明预热没引入交易，不证明预热后交易行为正确。
- 另外，被选中的本来就是"预热对结果没影响"的 case，有变化的 30 个都留红了。所以变绿的数量不能当作 B-2 语义已验证的证据。
- `extend_seed` 在 `warm=False` 时用 `main.iloc[0]` 复制出合成历史，带入了首根主数据的价格和成交量。这已在 docstring 声明，但应在报告里写明。

**5. 低中｜`test_short_pat1.py::http` 全局换成 `complete_seed`，审计只比金融字段**
- `shortpat1-audit.json` 里 index 4–43 和 78–81 是"失败且 `changed: []`"，只证明交易字段为空，没比 `error_type`/`error_message`。
- 旧路径是预热不足被拒绝。如果这些用例断言某个拒绝码，换成完整播种后原因可能变了，或者变成成功。
- 另外 66–77 这 12 个仍红，需要确认失败原因与 2840fa9 一致，不是新原因。time 审计有 old/new_error 对比，shortpat1 没有。

**6. 低中（需核对）｜`test_pin7e_final_close.py::run` 复制了 `p6.post` 主体**
- `_10b2d_cases.post` 的 provider_params 是 `{**base, **params}`。如果 `p6.post` 也有 `base`、fee、leverage 或默认 risk 字段，新 `run` 全部丢了，只剩 `params`。
- 另外 `run_id='p6'` 和 request 其余字段也要逐项对照。
- 否则"信号/成交/结算断言精确"建立在不同默认参数上。请 diff `p6.post` 与新 `run`，并确认 87/109 的失败集合相对 2840fa9 只减不增。

**7. 低｜eligible 的前缀剥离假设过宽**
- `time_`/`filter_` 前缀键被整体当作惰性，只检查了 `time_layer_enabled` 和 `filter_layer_enabled` 两个开关。
- 如果存在不受这两个开关门控的 `time_*` 参数（例如时间止损类），会被误判为 eligible。
- 运行期有 BASELINE 哈希兜底，但 allowlist 证据会误导。
- 建议显式列出惰性键，或加单测：剥离前后金融哈希相等。

**8. 低｜capture 脚本的自举和健壮性**
- `test_entry_filters` 和 `test_time_layer_compatibility` 在 import 时读取 `warmup_b3_disclosure.json`，而 capture 脚本顶部 import 它们并回写该文件。JSON 缺失时无法从干净树重录。
- `changed` 只遍历 `actual` 的键，字段被丢掉不会报。
- `warmup(*args)` 用 `args[3]/[5]/[6]` 位置取参，provider 若改成关键字调用会 IndexError。这只影响 eligible 路径，不会掩盖问题。

**9. 低｜frozen 金样边界缺机械守卫**
- 从 diff 看，两份 red_streak golden 只增加了 `indicator_warmup_bars:282` 和 `warmup_manifest*`，`result_hash` 与交易字段没变，和你的声明一致。
- 但 `protected-goldens.json` 只是一次性 sha 快照，测试里没有 pin。
- 它也没列 time/entry 的 BASELINE JSON 本体。`git status` 显示这些文件未改，但证据里没钉住。

## 已核对、未发现问题的部分
- allowlist 共 145 项，与 `time-audit.json` 里 `safe:true` 的 case 逐项一致，没有把 `safe:false` 的 case（chan_3buy/1、ema_cross/0、bollinger_squeeze/0 等）放进去。
- `time-audit.json` 里 `changed:["error"]` 的 case，old_error 和 new_error 文本相同。被排除是保守处理，不构成漏洞。
- `protected-goldens.json` 共 52 项，等于 51 个 B-2 金样加 warmup demo，与你的口径一致。
- `_10b2d_cases.post` 去掉 red_streak 特例后，其余 10b2d 金样字节不变，是合理的。
- 没有看到 xfail 或放松交易判据的改动。

## 结论
没有 Critical/High。建议先处理 1、2、3，补上 6 的对照，再跑一次全量。变异/还原证据尚未提供，我也没有运行任何验证。

### 作者处置（收尾全量前）

- 中1成立：新增对 assumptions/raw_report 的字段投影哈希，只允许 indicator_warmup_bars、warmup_manifest/hash 与 EMA warmup 披露改变；42组出现信号坐标、诊断或legacy_metrics变化的候选停止适配，allowlist由145收窄到103，保留原断言/失败。金融字段仍四项全等。
- 中2：single捕获现在写audit项，执行完整金融与非预热报告哈希守卫，并钉原min_bars。single原历史数据不存在统一engine哈希，不编造该证据。
- 中3成立：完整播种改为 complete=True 显式启用，只在entry/mtf关态测试和time关态/ledger测试调用；共享capture默认维持原路径。BASELINE deepcopy后覆盖，避免修改被其它套件引用的原字典。importer清单在/tmp/warmup-b3。
- 中低4：103仅为金融/非披露报告不变的旧夹具适配，零交易样本只证明不产生新交易，不宣称新版指标/生产读取验收；cold seed复制首根主窗OHLCV，明确为合成测试历史。
- 低中5：shortpat1直接http的26成功响应金融字段全等；旧主基线与候选44个失败响应逐项核验错误字段，12条共享capture未适配保留；最终与B-2失败nodeid对照另列。
- 低中6排除：亲读P6.post，无base或隐式费用/leverage参数；新pin run保留同一run_id/provider/request全部字段，只换已审计的flat seed；P6文件字节不改。101成功回包金融字段全等，失败集合收尾核对。
- 低7：惰性键改为注册的time/filter schema键，且必须等于各自default，不再任意剥离前缀；启用层或其它参数不命中allowlist。
- 低8：捕获声明从已提交完整夹具树运行；实际/预期键并集比对，禁止静默丢键。参数签名变动会显式失败，不隐藏。
- 低9：51个B-2金样逐字节守卫与原time/entry JSON未编辑取证；新red_streak两金样通过现成canonical捕获，金融字段变化0。

返修仅核受影响范围；未另跑第二轮外部review，不把作者核验记成Sonnet复审。最终全量、失败集合归因见B-3 verification文档和/tmp/warmup-b3。
