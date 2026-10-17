# PROVIDER-STRICT-CMP-1018（甲-1 / 走查 #14）

## 范围与基线

按派单从最新 `origin/main=18e5205b1501e411ba2bce1a6e8a82ad4e0acf97` 建独立 worktree / `fix/strict-threshold-cmp-1018`。
仅 provider；甲-2 服务端独立实施。三个完整工具 ID 均为 `local.backtesting_py.` 加下表工具名。
`threshold_inclusive` 只接受 bool，默认 True（省略同 True）；False 要求绝对值严格大于阈值。零 surprise 仍无方向、不交易。保留 Decimal 计算。
只推分支、开 PR 到 main，不合并、不发布、不改 pin。该仓未发现 AGENTS.md、CLAUDE.md、PROGRESS.md、BACKLOG.md；当前源码、README 与 Q18 契约为依据。

## 修改前逐点核查与取舍

以下代码行号按基线 18e5205；使用 rg -n 检索阈值与比较符，模板句式只读对照 TokenBeep/cutie-server/services/backtest_prompt_shapes.py（兄弟线可能继续修改该文件）。

| 判据 | provider 基线位置 | 模板句式位置及口径 | 决定 |
| --- | --- | --- | --- |
| 宏观 surprise | strategy_macro_events.py:278，`abs(delta) < surprise_threshold` 跳过 | shapes:893，偏差不小于 | 改，macro_surprise_direction 新参数可排除相等 |
| 资金费率 | strategy_macro_events.py:278 共用 surprise；strategy_funding_reversal.py:109–118 映射 rate_threshold_pct | shapes:920–921，费率不小于正阈值、不大于负阈值 | 改，funding_settlement_reversal 的 schema、解析、values、assumptions 一并接入 |
| FOMC move | strategy_macro_events.py:286，`abs(move) < move_threshold_pct` 跳过 | shapes:901–902，涨跌幅不小于 | 改，fomc_reversal 新参数可排除相等 |
| H1 区间突破 | strategy_macro_events.py:320/322，close > high / close < low | 严格突破，并非含等号阈值 | 不改；不接新参数 |
| 爆仓额 / 当日跌幅 | strategy_liquidation_reversal.py:416/424，value < threshold / drop > -min_drop_pct 跳过 | shapes:940–941，不低于爆仓阈值、跌幅不小于 | 不改，独立 liquidation 模板，超出 #14 三路径 |
| 看涨/看跌吞没实体比 | strategy_candle_patterns.py:152/154，body >= prior_body × multiple | shapes:517/841，实体不小于前根 1.2 倍 | 不改，独立形态判据 |
| 早晨/黄昏之星大实体 | strategy_candle_patterns.py:125，body >= mean × multiple | shapes:536/860，实体不小于均值 1.5 倍 | 不改，独立形态判据 |
| 看涨/看跌十字星全长 | strategy_candle_patterns.py:191，span >= mean_span × multiple | shapes:558/828，全长不小于均值 0.8 倍 | 不改，独立形态判据 |
| 恐贪买卖阈值 | cutie_backtesting_provider.py:5804/5807，value >= sell / value <= buy | provider 目录 :8539–8540 明确 at/below 与 at/above；非上述三句式 | 不改，独立分批策略 |

其他 ROC / ADX 等阈值本已严格比较，不改；不把参数合法性范围检查或止损/时间出场判据纳入本单。

## 实现与契约

- 三个 schema 同步加入布尔默认；TOOL_SPECS / 目录与白名单沿用 schema 派生，无另一份白名单需要手工维护。
- HTTP schema 校验在取数前拒绝 null / 数字 / 字符串 / 容器；MacroConfig 与 FundingReversalConfig 直接解析也拒绝非法类型。
- 两处交易判据复用 `_threshold_not_met`：inclusive 用 `<` 跳过，strict 用 `<=` 跳过。
- assumptions 回显有效模式；宏观 threshold 与资金费率 rate_rule 匹配实际比较。资金费率目录描述同步说明严格模式。
- 权威契约更新：`docs/Q18-macro-event-contract.md`。沿用旧跳过原因码，文档说明严格模式相等也属未满足判据。
- 甲-2 已核对完整工具 ID、参数名、默认行为、分支与本地路径。

## 直接本地验证

全部测试为真实 provider HTTP / 引擎执行、手工构造行情，市场与资金序列取数 mock；不代表真实远端取数、Pre、生产或服务端集成验收。
日志目录 `.runtime/strict-threshold-cmp-1018/`（gitignore，本地保留）：

- `fast.log/fast.exit`：`python3 -m pytest -q backtesting-py/tests/test_strict_threshold_cmp_1018.py`，85 passed，EXIT=0。
- `regression.log/regression.exit`：同命令运行 test_q18_macro_events.py、test_psf_3_macro_watching.py、test_p2_funding_settlement_reversal.py、test_p5_funding_central_db.py，248 passed，EXIT=0。
- 85 条专项覆盖 54 个三模板×双向×三模式×三边界场景、24 个 HTTP+直接解析非法类型场景、3 个目录契约、3 个零偏差和1个无结算点回显。

## 变异自证

先 `cp strategy_macro_events.py .runtime/strict-threshold-cmp-1018/strategy_macro_events.py.backup`。
把 `_threshold_not_met` 从 `return value < threshold if inclusive else value <= threshold` 改为旧行为 `return value < threshold`。
运行新文件 `-k 'public_threshold_boundary and equal and strict'`：6 failed、12 passed、67 deselected，EXIT=1（文件名包含 strict，因此 -k 也选中了等值默认/显式含等号的 12 条，均仍通过）；均在 `len(body['trades']) == int(enters)` 处证明错误入场。
失败节点为 `test_public_threshold_boundary[equal-strict-{sign}-{kind}]`，sign 为 -1/1，kind 为 macro_surprise_direction / fomc_reversal / funding_settlement_reversal。
再 cp 原备份还原并 cmp 确认一致，整个新文件回绿：85 passed，EXIT=0。变异/还原日志为 mutation.log/.exit、restored.log/.exit。

## 审查与剩余门禁

异模型只读审查：Claude `claude-opus-5-5`（effort high，Read/Glob/Grep 工具限定，无写工具），EXIT=0，PASS，无阻断项；全文见同目录 `independent-review.md`。
Codex 核实：审查确认默认兼容、双向精确边界、schema/解析、结果回显与范围均通过。两条低级建议：旧 below_threshold 原因码沿用（契约已明确包括严格模式相等）；FOMC 仅回显有效布尔参数、没有额外 gt/gte 文案（已有布尔值足以表达模式）。本单保留兼容行为，不扩展额外格式。
审查提及反向变异未做、HTTP 校验单层删除未做变异，均为证据边界；本单已实跑用户指定的旧判据变异，不将推测算作验证。
advisor 合前亲读仍由统领组织，本单不代签。甲-2 集成、同窗发布及生产验收不在本 provider PR 内。
