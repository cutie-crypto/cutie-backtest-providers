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
