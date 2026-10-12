# PROV-ARM-WARM independent Claude review

Baseline d7c7467. Read-only static review; execution evidence collected separately.

## claude-review.json / claude-sonnet-5-5

我读不到 `/tmp/prov-arm-warm/review.diff` 和任务书，这两个路径被权限拒绝。下面的结论只来自对工作区源码和新测试的静态审读（`cutie_backtesting_provider.py` 的五个 builder、`:11164-11256`、`_fetch_template_warmup`，以及 `test_arm_ema_warmup.py`）。我没跑任何测试，没看 `baseline-changes.json`、元数据 fixture 和 `test_time_layer_compatibility.py` 的改动。

**Finding（1 条，低到中）**
- **位置：** `cutie_backtesting_provider.py:11178`，起因是 `:4885`（ema_rsi_pullback）和 `:5330`（ema_trend_rsi）。
- **问题：** 预热取数改成 `built.get("ema_warmup_target_bars", min_bars)`，目标只含 10×EMA。这两个模板的 `min_bars` 带 RSI（`rsi_period+1`），所以预热根数可以低于 `min_bars`。
- **触发条件：** `rsi_period+1 > 10×EMA周期`。builder 只限制 `ema_period≥2`、`rsi_period≥2`，没有上限；例如 `ema_period=2, rsi_period=100`，旧逻辑预热 101 根，现在 20 根。
- **影响：** RSI 的 Wilder 播种变浅，入场信号可能和基线不同。`min_bars` 闸门仍靠主区间根数放行，所以不会报错，只是悄悄变化。
- **建议：** 取 `max(target, min_bars)`。或者确认公开 schema 的参数范围让这种组合不可能出现，并加一条测试固定下来。

**逐项核对（静态）**
- **requested = 10×最长 EMA（RSI 不乘）：** 五个 builder 的 requested 与 `target=min(requested, MAX_EMA_WARMUP_BARS=20000)` 都与 `ema_cross` 同构。
  - triple 用 `long`，ema_trend_rsi 用 `ema_slow`，ema_rsi_pullback 和 bias 用 `ema_period`，ema_pullback 用 `max(fast, slow)`。
  - `min_bars` 公式和 `next()` 里的入/出场代码，在我读到的段落里没看到改动。
- **取数和播种接线：** `:11178` 取数后，`:11252-11256` 把预热段挂成 `_warmup_bars` / `_warmup_cols`，各 `_warm` 指标都能吃到前缀。
- **预热段不产生信号：** 预热前缀不进 `self.data`，只有主区间会进 `next()`。测试用 `min(evaluated) >= START` 钉住了这一点。
- **历史不足或不可用：** 沿用 `_fetch_template_warmup` 的 best-effort，异常返回空预热，与 `ema_cross` 一致。
- **result.v2 键集：** `ema_warmup` 只加在 `raw_report` 和 `assumptions`。测试断言 `metrics`、`data_manifest`、`trades` 不含该键，并检查 `V2_KEYS`。
- **范围提示（非 finding）：** 带 range/calendar/event 等配置的分支（`:11176`）不取 EMA 预热，报告里 `actual_bars` 会是 0。这和 `ema_cross` 原有行为一致，请确认是有意保留。

**测试评价**
- **真实播种：** 能钉住。测试用独立的 EMA/BIAS 递推（从 `closes[0]` 起，前缀 `target+360`），逐点与策略指标比对，容差 1e-10，并校验 fetch 区间。
  - 它没独立核对 RSI，这不算缺陷，因为任务要求 RSI 不乘。
- **cap 分支：** 只在 builder 层把 `EMA_WARMUP_MULTIPLIER` 打补丁成 2000 来验证 `target=20000`。没有端到端用例：既没验证 `truncated=True` 的报告和提示文案，也没验证实际取数不超过 20000 根。
- **不足历史：** 覆盖了 0、15、50 根和取数异常。`warm_count` 取 50 时对 period 为 60 或 50 的 case 仍不足；而 15 和 50 都只断言报告，没断言指标值。
- **缺失用例：** 没有 `rsi_period+1 > 10×EMA` 的用例，这正是上面 finding 的盲区。

## claude-review-final.json / claude-sonnet-5-5

静态审查结论（只读；Bash 被禁用，没有跑任何测试、没有读 baseline-changes.json，也没读 fixture 内容）：

**证据核实：准确**
- `ema_rsi_pullback`：`ema_period` 50–300、`rsi_period`≤100（provider:8357-8358）。RSI 最大 min_bars 为 101，小于 10×50=500，所以上轮「RSI>10×EMA 缩短预热」对该键不成立。
- `ema_trend_rsi`：`ema_slow`≥3、`rsi_period`≤100（:8411-8412）。slow=3 加 RSI=100 合法，此时 target=30 而 min_bars=101，这个边界真实存在。
- 其余四键 min_bars 都不大于 10×最长 EMA：triple 的 min_bars=long，pullback 的 min_bars=ema_slow，bias 的 min_bars=period。triple 校验了 short<mid<long，所以 `10×long` 就是最大 EMA 的 10 倍。

**Finding**
1. **中｜`cutie_backtesting_provider.py:11178` + `:5330`**
   - 触发：`ema_trend_rsi` 满足 `rsi_period+1 > 10×ema_slow`（如 slow≤10、RSI 取 100）。取数深度从 `max(ema_warmup_target_bars, …)` 取值，没有 min_bars 兜底。
   - 影响：原先取数深度是 min_bars=101，改后只取 30 根。RSI 的预热变浅，`_warmup_bars+len(data)<min_bars` 的入场闸门可能多挡主区间头部信号，结果会比旧 pin 差。这是对合法参数的行为回退，不是「只增预热」。
   - 现状：已有 `test_trend_rsi_long_rsi_retains_taskbook_ema_only_target`（test_arm_ema_warmup.py:135）锁住任务书口径，没有擅自改口径。我建议等统领裁决。授权后的最小改法是只在取数目标用 `max(target, min_bars)`，report 的 requested/target 不变。
2. **低｜`README.md:75-82`**
   - 触发：README 写「RSI periods do not contribute」，但没提 RSI 更长时取数会比旧版更浅的边界。
   - 影响：服务端或使用者可能误以为预热只增不减。裁决前至少补一句披露。
3. **低｜`cutie_backtesting_provider.py:11250`**
   - 触发：截断文案写「请调小慢线周期」。
   - 影响：五键的公开 schema 上限都到不了 20000（`ema_trend_rsi` 最大 4000）。HTTP 截断分支只能靠 monkeypatch 触发（test_arm_ema_warmup.py:31），线上不可达，对 bias/pullback 的「慢线」措辞也不准。无需改码，回报里注明即可。
4. **低｜`test_entry_filters.py:43-49`、`test_time_layer_compatibility.py:65-69`**
   - 触发：14 case 的指纹按「engine/result/min_bars/warmup_bars 与生成前完全相等」比对。这说明夹具历史不足，没有走到深预热。
   - 影响：指纹只锁得住 assumptions/raw_report 的披露，锁不住「预热变深后的交易结果」。真实行为覆盖全靠 `test_sufficient_history_seeds_indicators…`（:44）。可以接受。两处断言的 key 集（7 名×2=14，单例 6 个）与改动自洽。

**漏改风险**
- 任务书点名要核的 `test_warmup_gate_legacy_templates.py`、`test_template_indicator_warmup.py` 不在 diff 里（后者:184-198 用五个模板）。是否因预热加深而不需要改，只能靠实跑确认，我这里没有证据。
- 其余范围（五键 builder、min_bars 不变、入场出场逻辑未动、未碰 result.v2 键集）符合任务书和 22:1x 裁决。

其他无新发现。

## Implementation verification

- The short EMA/long RSI boundary is valid for ema_trend_rsi only. Requested command ruling remains pending; shared fetch and trading logic were not altered. README discloses the boundary.
- Existing public parameter maxima do not reach the cap. Five HTTP tests simulate larger requests via the shared multiplier and verify fetch depth, truncation reports and cap disclosure.
- Time/filter snapshots inject fixed 0/60 bars. Filter snapshots preserve engine_trades/engine_equity/result_v2/min_bars/warmup_bars, changing only report hashes. All old/new values are archived in the adjacent baseline-change JSON. Real HTTP tests independently validate all EMA/BIAS indicator points using a scalar recurrence and exercise main-range trades.
- Removing the five new request assignments and two return keys from each builder yields an AST identical to d7c7467 for the entire provider file. Entry/exit/min_bars/downstream AST is unchanged.
- Initial five-file suite: 17 failed/702 passed (5 invalid new schema parameters, 12 old report fingerprints); corrected:719 passed. Extended filter suite:28 failed/1584 passed (report fingerprints); corrected:1617 passed. Final 41 new tests passed, including the legitimate short-EMA/long-RSI boundary.
- Triple mutation removed both EMA return keys: test_builder_tenfold_depth_and_original_minimum[ema_triple_alignment] and test_sufficient_history_seeds_indicators_without_warmup_signals[ema_triple_alignment] failed (EXIT=1). Restored with cp and verified cmp EXIT=0; six-file restoration suite passed.
- Serial full-suite result and final command ruling will be appended at closeout. No TokenBeep modifications, pin update, main merge, Pre/production/device validation.

## Final sibling review / claude-sonnet-5-5

我在静态核对两份 fixture,不运行测试。已读基线变化 JSON 的 20 个 moved_leaves 案例(patconf 10、plow 10)、代表条目、全部 leaf 路径和各案例目标值。`arm_ema_warmup_response_overrides.json` 约 12 万 token,我没通读,只靠它的变化记录间接核对。

**结论:未发现新问题。**

- **10× 播种:** 目标值与 `10×最长 EMA 周期` 一致,RSI 周期不参与,且都未触及 20000 上限。`bias_reversion` 是 100 和 200(周期 10 和 20),`ema_pullback` 是 300 和 600,`ema_rsi_pullback` 和 `ema_trend_rsi` 是 2000,`ema_triple_alignment` 是 600 和 1200。两组 patconf 和 plow 的参数不同,所以同一工具的目标值也不同。
- **没有放宽断言:** moved_leaves 只涉及 `.assumptions.ema_warmup`、`.assumptions.indicator_warmup_bars`,以及 `.raw_report.ema_warmup.{requested,target,actual}_bars`、`truncated`、`tenfold_reached`。没有任何 trades、equity、metrics 或 data_manifest 的叶子被改。对应的 patconf 和 plow 响应快照只被覆盖了这五个工具各 on/off 共 20 例,原 golden 没有动。
- **数值一致:** `actual_bars` 是 59 或 60,小于目标,所以 `truncated=false`、`tenfold_reached=false`。这符合有限前缀的尽力而为策略。`indicator_warmup_bars` 从 10 或 20 变成 60,也符合前缀变深。
- **指纹未变:** 12 个 `filter/*` 条目里 `min_bars`、`engine_trades`、`engine_equity`、`result_v2` 的哈希新旧相同,只有 assumptions 和 raw_report 的哈希变了。
- **测试改动:** isolated 比较器只 `pop` 这两个新字段,再走原有的逐字节比较。目标值 600/1200/200/2000/2000/600 与 builder 公式一致。

**Info,不属于缺陷:** moved_leaves 的 `reason` 写的是"derived indicators/results change",但记录里没有一个结果叶子变化。这是措辞偏宽,不影响判断。如果要写得精确,可以改成"仅报告字段变化"。

Implementation: first full suite37 failed/10595 passed, all37 from three historical report comparators. Four-file repair481 passed EXIT=0. All20 response override cases change only assumptions/raw_report leaves; no trades/equity/metrics/manifest leaves change. Overbroad reason text corrected. Second full suite running after those repairs; pending result.
