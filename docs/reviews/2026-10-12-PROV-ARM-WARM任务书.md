# PROV-ARM-WARM：provider 给 A2 两键 + A3 两键 + A1 bias 共五键补 ema_warmup 10×（一次 PR、一次 pin），≤50 轮

常备令适用（`.claude/rules/herdr-line-standing-orders.md` §1/§2，但本单在 provider 仓）；命令重定向到文件 + EXIT，不接管道；大文件 `grep -n` 定位后按段读，禁止通读（provider 主文件 1 万多行）。到 40 轮提交已完成部分并回报。

## 背景（统领 1012 裁）
ARM-A2（ema_triple_alignment、ema_trend_rsi）与 ARM-A3（ema_rsi_pullback）要接实盘布防；服务端判定器按 10× EMA 历史播种，provider 这三个 builder 目前没有 ema_warmup，用 min_bars 起算，EMA 残差约 13.5%（e^-2），服务端约 2e-9，同组 K 线逐根对不上。补齐后服务端金样在新 pin 上生成。三键攒一次 PR、一次 pin；pin 由 ops 经统领放行，本单不碰 pin、不碰 TokenBeep 仓。

## 范围（行号按 origin/main d7c7467，先 grep 核实）
工作区：`/Users/tf/Documents/bitbucket/cutie-backtest-providers-armwarm`（分支 feat/arm-ema-warmup，基于 origin/main d7c7467），文件 `backtesting-py/cutie_backtesting_provider.py`。
1. `_build_ema_triple_alignment`（:6161）：ema_warmup_requested_bars = EMA_WARMUP_MULTIPLIER × long。
2. `_build_ema_trend_rsi`（:5290）：= EMA_WARMUP_MULTIPLIER × ema_slow（只算 EMA，RSI 不乘）。
3. `_build_ema_rsi_pullback`（:4860）：= EMA_WARMUP_MULTIPLIER × 该模板所有 EMA 周期的最大值。
- 写法照 ema_cross 一族（:3621 与 :3659-3660，常量 :3819-3820）：built 里同时给 `ema_warmup_requested_bars` 与 `ema_warmup_target_bars = min(requested, MAX_EMA_WARMUP_BARS)`；下游 :10692 / :10745 起的取数与 ema_warmup_report 按现有通路自动接上，先读懂 ema_cross 在历史不够时的行为，三键与它同行为，不另发明。
- min_bars 不改（triple=long、trend_rsi=max(ema_slow, rsi_period+1)、不加 +1）；入场 / 出场逻辑一行不改。
- 新信息只走现有 ema_warmup_report / raw_report 通路，不进 result.v2 的 trades / metrics / data_manifest 键集（服务端严格相等校验，见 TokenBeep `.claude/rules/dispatch-pitfalls.md` 第 30 行）。
- **统领 22:1x 裁：五键一次补齐**——另加 4. `_build_bias_reversion`（:6108）：= EMA_WARMUP_MULTIPLIER × ema_period；5. `_build_ema_pullback`（先 grep 定位）：= EMA_WARMUP_MULTIPLIER × 该模板所有 EMA 周期的最大值。一次 PR、一次 pin。回报第 8 项把新 pin 上需要重生的服务端金样 / parity fixture 全列出来（含 A1 的 BIAS 金样，现按旧 pin 生成）。

## 用例（照 `backtesting-py/tests/test_ema_backtest_warmup.py` 的形状）
每键：requested / target 数值正确；超 MAX_EMA_WARMUP_BARS 时 target 封顶；历史足够时预热段不产生信号、结果正常；历史不足时与 ema_cross 同行为。再检查 `test_warmup_gate_legacy_templates.py`、`test_template_indicator_warmup.py`、`test_time_layer_compatibility.py` 里这五个模板的基线，结果变了的照实更新并在回报第 7 项逐条写原值→新值和原因。
变异：去掉 triple 的 ema_warmup 键 ⇒ 指名哪条红；还原用 cp 备份，恢复后重跑回绿。

## 门
定向 `python3 -m pytest -q -p no:cacheprovider tests/<文件>` → 收尾全量串行一次 `python3 -m pytest -q -p no:cacheprovider tests`（约 10 分钟，不装 xdist，不带 -n）。git 一律 `git -C <worktree>` 单独一条命令，不 `cd && git`。

## 提交与 PR
中文 commit、无 Co-Authored-By。推分支 feat/arm-ema-warmup 并 `gh pr create --base main`（main 必须 PR）；**不合并 PR**，合并由 impl 亲读后做。异模型 review 一轮：agy 或 Claude（`codex review --base` 不能带 PROMPT，见 dispatch-pitfalls 第 76 行），落档写进 PR 描述。

## 回报
首行 `PROV-ARM-WARM-DONE <7位sha>`，八项 ≤25 行（第 2 项写 PR 号与链接），写 /tmp/prov-arm-warm/REPORT.md；第 8 项写清楚：新 pin 上服务端哪些既有 parity / 金样 fixture 会因这三个模板结果变化而需要重生（文件名）。回报后在本 pane 回一句「PROV-ARM-WARM-DONE <sha>」。
