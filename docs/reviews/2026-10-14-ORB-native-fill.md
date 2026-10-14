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

独立异家族 review、完整 suite、最终 SHA 及 Advisor 亲读：待续记。完整 suite 必须 A 排窗，命令 `/private/tmp/LONGWIN-4/provider-venv/bin/python -m pytest -q tests`，cwd 本树 `backtesting-py`，当前 RESULT→A 客户集成门优先。无生产/QA 浏览器/凭据/归档查询。
