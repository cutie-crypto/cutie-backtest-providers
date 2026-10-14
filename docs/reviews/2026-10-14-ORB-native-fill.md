# ORB 原生保护成交候选（2026-10-14）

范围：本地候选，base `0da4fc7c6114fd25ddb85c6dfa8129a5fbb8a85f`，分支 `impl/prov-orb-native-1014`；不创建 PR、不 push/合入/发版/pin/生产 direct。A 17:09 亲核隔离探针后许可本地实现；C→Advisor 提 PR 前亲读尚未通过。

Advisor 原裁与 C 协调全文见 `/tmp/tokenbeep-b1-1014/orb-advisor-implementation-ruling.md`，仓内 authority 为 TokenBeep `2f31b7c33:docs/plan/当前批/待裁.md:91` 起。原裁分开保存，不把 chat 转述提升为 Owner 聊天原件。

成交价规则：SL 保留冻结区间另一侧；TP 保留实际入场成交价 ± 冻结区间高度 × 倍数；计算输入及表达式仅从原 `_risk_layer_check_exit` 移到 `_risk_native_state`，无算法公式改变。`RangeConfig.profile` 只在 `orb` 启用 `_template_native`，Asia 保留旧 next-open。公共 risk mixin/broker/provider 大文件、参数源、能力目录、固定金融金样、默认风险和 health 均未改。

调用链：range `init()` → 原 `_risk_init/_risk_native_install`；原 broker `protected_process/arbitrate` → range `_risk_native_state`；新 entry 的原 `on_open` → `_risk_native_state/_risk_native_protect` → 原同根第二次 process；原 strategy next 仍判到期并提交 market close。仅在原 `_holding_expiry().due` 为真时，在 broker 处理前撤该 trade 的 TP，保留 SL；entry 当根的 protect 也执行同一检查。未新增未来 OHLC 读取或 public broker 排序。

到期判据使用既有 entry bar、当前 bar count、冻结时间配置和当前决策时钟。SL → 时间到期 → TP → 模板信号是本次新顺序；时间和 signal 的 `position.close()` 仍 next-open。ORB 没有新增模板信号出场分支，测试用真实子类注入既有 market close 来验证其顺序和成交时点。

精确 pin14 参照（源在 `/tmp/tokenbeep-b1-1014/ORB-native/pin14-ref/`）：`d7c7467:backtesting-py/cutie_backtesting_provider.py:3498–3505` 共用 TP/SL 挂价；`:3513–3585` 原生安装、open/reduce、arbitrate、同根第二次 process；`:3222–3224` 明确旧 generic TP 先于 max_holding。引擎为 `backtesting==0.6.5`，没有把当前 main 当 pin14。

与 pin14 一致的真实引擎场景：long/short × 普通 SL、SL gap、同根双触、普通 TP、TP gap、entry SL、entry TP、entry 双触，共 16 例；EntryBar/EntryPrice/ExitBar/ExitPrice/exit_kind 逐项对照。原参照输出 `pin14-real-output.json`，EXIT0。按新裁明确不同的场景：flatten/max_holding/entry 到期与 TP 碰撞，时间胜 TP，SL/gap 仍胜时间；不声称全部退出优先与 pin14 一致。

进入实施条件的隔离探针：`order-probe-trace.json`、`execution-order-evidence.txt:3–10`、`order-probe-summary.json`，8 个新优先场景及 16 个 pin14 对照 EXIT0；trace 证明 TP 撤销先于同根真实 engine process，entry 仍仅一次，其他订单身份与 SL 留存不变，Asia 原 next-open 及未来末根 OHLC 变异不影响已到期成交。它只是 A 许可本地实现的条件，不替代正式候选门或 review。

统一验证解释器 `/private/tmp/LONGWIN-4/provider-venv/bin/python`：Python 3.12.13 / backtesting 0.6.5 / pandas 2.3.3 / numpy 2.5.4 / pytest 9.1.1；system Python 3.9.6、numpy 1.26.4、pytest 8.4.2 不作门。private 环境无 pip 模块，pip check 未运行成功，未安装或修改全局。

本地轻门：原 F1 基线 61 passed / EXIT0；新增原行为红证 17 failed / EXIT1。首次正式 F1+新增聚焦 6 failed / 92 passed / EXIT1，全部是旧 next-open 断言；保留失败日志，仅更新获准 ORB 断言，未改金融金样。补 short/risk-switch/order trace 后 114 passed / EXIT0。反 next-open 变异（`_template_native=False`）17 failed / EXIT1；原字节恢复 SHA256 一致，114 passed / EXIT0。日志均在 `/tmp/tokenbeep-b1-1014/ORB-native/`。

PR 写集交叉（实际 PR API 读取，不把 remote branch 最新当 PR）：#103 `impl/longwin-4` head `bcbd7240ae590f6e4596c015ba051b299dab5904` / base `0da4fc7`，5 文件，provider 仅 catalog bars、范围校验和预取数闸门；#104 head `3e3e2010451771a659e7b5608bcd3ded2858938e` / base `0da4fc7`，6 文件，仅 PUMP catalog/spec。与本单无生产函数交集，README 同文件建议由 A 串行局部合入。先前 `impl/warmup-b@1ff49f7` 是另一支，保留初读记录，不作为 #103 证据。

文案：ORB assumptions 将原生保护与 next-open market 分列，README 同步；未修改 Asia 的 fill/note。客户发布页仅建议待发布，生效时间和版本须取实际部署，历史 run/报告不回写；“重跑”不授工程新增 Fan 预算。TokenBeep `docs/guides/BACKTEST-COVERAGE.md:50` 仍有通用旧 next-open 声明，建议 A 另树授权按最终部署范围同步，本线未越界修改。

独立异家族首审：实际 `opencode-go/deepseek-v4.1-flash` / agent `plan`，session `ses_ec683e8a9ffe8q2pRKLPr5uoKS`，CLI EXIT0、export EXIT0，实际工具仅 read/glob/grep。原结论为“算法段 PASS；完整候选验收 FAIL”，原 report/export 不覆盖，完整原文见本页末尾。

返修边界：中-2 补 long/short × leverage=2、按风险 1%/step .001、固定名义 1000 六条真实 HTTP engine 回归及 leveraged period-end 一条；低-4 分开 `entry_trigger=closed_bar_breakout` 与 `trigger=protective_intrabar_high_low`，README 同步。121 passed / EXIT0。新增测试初版误读 result.v2 trade 字段（7 红）及公开 `end_of_data` 与内部结算 reason（1 红）均留日志；按现有协议修正测试，没有修改产品协议。

中-3 不是本批改写风险模型：range 原有 RiskState 构造逐字移动，原有动态/levels 定价键继续拒绝，本批不扩能力、不改公共状态规则；低-5 尚无已证 reason 污染、低-6 已在 README 注明 generic 与 ORB 例外，不修改公共 mixin。

完整 suite **NOT_RUN**：A 17:18/17:34 明确无窗口，RESULT→客户集成优先，不执行或采样。旧 ORB 字节 fixture 与新 assumptions 存在静态冲突（首审高-1），不能记成真实 full EXIT1。旧 golden/fixture/xfail 保持原字节；A 另定新 pin/新金样依赖及 supersede 方式后才跑全门，本线不实现迁移。完整命令为 `/private/tmp/LONGWIN-4/provider-venv/bin/python -m pytest -q tests`，cwd 本树 `backtesting-py`。

最终异家族返修闭环、最终 SHA 与门绑定及 Advisor 亲读：待续记。候选可送 Advisor 亲读成交段，尚非 PR/上线许可。无生产/QA 浏览器/凭据/归档查询。

## 独立首审原结论（原 FAIL 保留）

I'll perform the independent read-only audit. Let me gather all the materials first.

独立异家族只读审查完成。以下为结论（真实模型：`opencode-go/deepseek-v4.1-flash`；仅读 read/grep，未写文件、未起 agent、未跑 run/测试）。

## 1) 结论：算法段 PASS；完整候选验收 FAIL（阻断）

- **算法/成交段 PASS**：`strategy_range_breakout.py` 的 native 保护实现与裁 (a)(b)(c) 一致，无 in-scope 逻辑缺陷（价格公式为逐字位移，优先级 SL→时间→TP→信号成立，到期撤 TP 在 broker 之前）。
- **完整候选验收 FAIL（阻断）**：只跑了 F1+新增的 114 聚焦集；全量 suite 未跑，而 ORB `range_assumptions` 的改动必然打破仓内多个**冻结 ORB 字节基线**（见高-1）。按裁「0da 旧金样不混、新 pin 由 A 另定」，这些金样本批不得改，故候选现状不可能全绿。
- **进入 Advisor 亲读：满足**（针对成交函数段）；不代表 PR/上线许可，也不代表完整验收。

## 2) 发现

**高-1｜冻结 ORB 字节基线必然变红，且本批无新 pin（阻断完整验收）**
- 触发：跑全量 `pytest -q tests`。
- 因果：`range_assumptions` 对 ORB 改写 `fill/note/priority/trigger` 并新增 `market_fill`（`strategy_range_breakout.py:277-282`），这些字段被整份 body 逐字节钉住：
  - `tests/golden_10b2d/opening_range_breakout.default.json:1`（及 `.short.json`）内 `"fill":"next_bar_open_market"`、旧 note、`"priority":"stop_loss_before_time_expiry_before_take_profit"`；比较点 `tests/test_10b2d_sizing_range_calendar.py:39`。
  - `tests/fixtures/7p3b2_off_c72c4a1.json:9-10`（ORB default/both）；用于 `tests/test_7p3b_pattern_range_filter.py:24,220-231`。
  - `tests/fixtures/patconf1_5ddd8eb.json:74,121`（off/traded ORB）。
  - `tests/fixtures/isolated_off_waveb_e698d26_6b85dbf_9f8a6b0.json:111-158`。
- 影响：至少上述 nodeid 变红；聚焦 114 未覆盖这些模块。
- 最小修法：**不改金样**；由 A 的新 pin/金样计划显式 supersede（裁已预留），或在文档中把「全量未跑+金样待重钉」列为硬前置。

**中-2｜ORB 现在无条件接入共享 native broker 包装，杠杆/sizing 路径未被本批覆盖**
- `cutie_backtesting_provider.py:3266-3269`：`_risk_native_enabled = getattr(self,"_template_native",False) or (...)`；ORB `_template_native=True`（`strategy_range_breakout.py:109`），故 `risk_layer_enabled=false` 也会 `_risk_native_install()`（:3305、:3523），并可能叠加 `_isolated_install_settlement`。
- 触发：ORB + `leverage>1` 或 `position_size_*`。
- 影响：ORB 的最终 bar 结算/杠杆/liquidate/sizing 环绕路径由「旧 next-open」变为 native（与其它 native 模板同构，缓解但不等于已验证）。`test_orb_*` 只切 `risk_layer_enabled`，未测 ORB+杠杆/sizing。

**中-3｜`_risk_native_state` 完全替换 mixin 版本（未调 super）**
- `strategy_range_breakout.py:169-189` 不复用 `initial_risk_state` 的 sizing-state/`original_units` 与杠杆 `_isolated_stop_beyond_trades` 计账。
- 影响：对 ORB 出场无实质影响（levels 空、stop 取冻结区间），但偏离 mixin 约定；如未来 ORB 启用动态止损/分批会出问题。当前低-中。

**低-4｜`trigger='intrabar_high_low'` 措辞**（`strategy_range_breakout.py:278`）：入场仍是闭 K 突破，仅保护为盘中触发；字段含义可能被误读。

**低-5｜`_risk_exit_reason` 不按 cycle 复位**（仅在 `init`/`_risk_init` 置 None）：当前所有读取点都先写入，未见错误 reason 污染；属潜在脆弱点。

**低-6｜mixin docstring 仍写通用「TP 先于 max_holding」**（`cutie_backtesting_provider.py:3218-3232`），与 ORB 新序相反；README 已注明差异，非矛盾，但易混。

## 3) 测试缺口
- 全量 suite 未跑；上列金样模块未纳入聚焦集。
- 无 ORB+`leverage>1`、ORB+`position_size_*` 的 native 覆盖。
- 无「Asia 在 `_template_native=False` 下整链 native 关闭」的多场景字节对照（仅 1 条 Asia next-open 用例）。
- pin14 对照/新优先/变异证据（EXIT0/17红）均由**被审方自跑**，本次只读无法复现；已核日志与哈希（`mutation-before/restore-hash.txt` 同为 `302e…bd64e`，17红/114绿一致）。

## 4) 跨模板影响 / 恢复风险
- 生产改动仅在 range 模块；provider/mixin/broker 未改 → Asia 及其它模板代码路径无变化，回退干净。
- 主要风险是**金样重钉**：`README.md` 与 PR103/104 同文件（`actual-pr-overlap.json`：交集仅 README），由 A 串行局部合入。
- pin14 行号引用（:3498–3505、:3513–3585、:3222–3224）经查指向 `pin14-ref` 文件，正确；未把当前 main 当 pin14。

## 5) 是否满足进入 Advisor 亲读
满足（限定于成交函数段）：建议 Advisor 亲读 `strategy_range_breakout.py:169-206` + `_risk_native_install/arbitrate`（`cutie_backtesting_provider.py:3523-3588`）+ `_risk_layer_check_exit`（:3603-3648），并核对 `candidate-order-trace.json`（bar11 撤 TP 在 engine_process 前、entry 仅一次、market parent 保留）。**进入亲读 ≠ 完整验收**：全量 suite 与新 pin/金样必须另行放行，本批不得改冻结金样。
