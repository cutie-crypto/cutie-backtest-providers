# 62-2a Provider compiler / kernel / capability 复核记录

日期：2026-07-15
分支：`codex/62-2a-provider-kernel`
基线：`8cfe18e5fd7d82c50f7f5079e4171b4e333352dd`

## 结论

本分支已实现 62-2a 的 Provider 本地基础：完整 StrategySpec v2/manifest 静态编译、共享 capability/hash、artifact execution wire 校验、历史 replay 与 future paper 共用的单一 `StrategyKernel.evaluate`、result.v2/trace/evidence 构造，以及 legacy 七个固定策略的兼容路由。

当前结论为 **Source + Local tests Pass，交付 Owner-gated / Blocked**。共享 capability fixture 广告了三种 synthetic transform，但本分支没有实现它们；这会造成 capability overclaim，不能在未解决合同冲突前合并、部署或宣称 62-2a 可用。当前 commit 仅保存 blocked candidate。本分支也没有部署 Provider、没有接触 Pre、Fan、RN 或真实中心数据，因此这些证据均为 **Blocked / Cannot assess**，不能由本地通过替代。

## 冻结合同与实现边界

- 只读对照 TokenBeep 的 `SPEC_策略工件与统一执行契约.md`、`IMPL_策略接入与不可变版本.md` 和共享 capability fixture。
- Provider 本地 fixture 与当前共享 fixture 一致：`binance_futures`，SHA-256 `1f6ad3b031a1854667781577dd74b5ffa8dfca6e204632ef4e5e6316b9ada05e`。该通用 capability 的 Owner 签字当前未找到，不能把 fixture 存在解释为 Owner 已批准。
- 只有 `local.strategy_spec_v2.compiler` 在 immutable lowercase revision 下出现并声明 capability；七个 legacy tool 不声明 artifact capability。revision 未锁定时 compiler tool 不进入 catalog。
- artifact-shaped 或 partial artifact 请求一律 fail-closed，不回退 fixed tool、ccxt 或默认策略。
- 当前共享 capability 仍是 exact subset（binary `add`、`gt`、`crosses_above`）。编译器和 kernel 已实现 `add/mul/min/max` 变参 `>=2`、`sub/div/compare/cross` 固定二元、`abs` 固定一元；只有 capability 精确出现实际 `arg_types` 长度的签名时才可执行，不从二元签名推断三元签名。

## Owner-gated 合同冲突

- `data_requirements[*].allowed_transforms` 会被原样投影到 `capability_requirements.data_transforms`，compile 只检查它是否是当前 capability 的成员。
- 当前 capability payload 无条件列出 `combine_first.v1`、`ffill_after_close.v1` 和 `flow_dilution_shifted.v1`，所以带这些 allowed transform 的 artifact 会 compile 通过。
- execution 没有 transform dispatcher 或 synthetic frame builder；exact rows 完整时可以不使用 transform 而成功，出现需要 synthetic 补齐的缺口时直接返回 `ERR_STRATEGY_COVERAGE_INCOMPLETE`，coverage 的 `transforms` 永远为空。
- 因 capability 在 SPEC 中表示实际支持，这不是可接受的“安全降级”，而是 compile 可用性判断的假阳性。最小兼容方案是由 Owner 决定：要么在 62-2b 实现和零前视 fixture 完成前从共享 capability/fixture 移除三项并生成新 hash；要么把三种 transform 及其签字参数来源纳入当前交付。当前不能猜测修改。

## 本地实现检查

- StrategySpec/manifest 顶层与嵌套 exact-key、排序、静态类型、hash/binding、data requirement 和 source material 校验。
- capability 顶层/嵌套 exact-key、16 KiB、数组上限、排序去重、静态类型与真实 operator arity 校验。
- Decimal128 precision 34 / ROUND_HALF_EVEN；integer safe range；next-open、tick/qty、long/short gap、same-bar priority、三种 sizing、双边 fee/slippage、time/signal/end-of-data 语义。
- closed FeatureFrame、rolling_sum、lag/cross 防前视；同一 source/interval 可绑定多个 feature，不互相覆盖。
- execution request 的 11-key exact-set、Snowflake string、artifact/spec/manifest/capability/revision/result-contract 绑定；Server 注入的 trusted `instrument_rules` exact object。
- result.v2 五键、trade 十键、coverage/trace/evidence 和各 canonical hash；中心数据声明不匹配、空流、gap、未覆盖完整对齐区间均失败关闭。
- validator 仅对白名单字段放行 7..64 lowercase `provider_revision` 与 64 lowercase `strategy_execution_capability_hash`；同形状值放在无关字段仍被 secret scan 拒绝。

## 验证证据

| 层级 | 结果 | 命令 / 证据 |
|---|---|---|
| Focused compiler/kernel/API | Pass | `python3 -m pytest -q backtesting-py/tests/test_strategy_kernel.py` → `35 passed`；其中一条 blocker reproduction 明确证明 allowed transform compile pass、gap execution coverage fail |
| Provider + validator + installer tests | Pass | `PYTHONPATH=validator python3 -m pytest -q backtesting-py/tests validator/tests scripts/tests --import-mode=importlib` → `156 passed` |
| Format | Pass | Black check：新 Strategy 模块、focused tests、validator secret scanner；isort `--profile black` 同范围 |
| Lint | Pass | flake8：新模块/tests/validator；既有 Provider 文件沿用仓库现状并忽略既有 `E741,F841`，其余通过 |
| Diff hygiene | Pass | `git diff --check`；提交前另跑 `git diff --cached --check` |
| Pre / deployed Provider | Blocked | 未部署、未读取节点 revision、未调用真实 catalog/backtest |
| 中心数据 / transform | Cannot assess | 未调用真实 Binance Vision/中心 K 线/CoinGlass；无 Provider runtime evidence |
| Fan / RN | Cannot assess | 不在本分支执行范围，未接触设备 |

测试仅出现 FastAPI `on_event` deprecation、macOS LibreSSL/urllib3 三条既有 warning，不改变断言结果。

## 残余风险与后续边界

1. 62-2b 的 coverage 目录、真实 freshness/gap incident、`combine_first.v1`、`ffill_after_close.v1`、`flow_dilution_shifted.v1` 及零前视 fixtures 尚未实现。本分支只允许 exact complete streams；需要 synthetic transform 时会 fail-closed，不能视为 62-2b 完成。
2. 当前共享 capability/fixture 与实现存在上述 overclaim，且通用 capability Owner 签字未找到；这是合并/验收 blocker，不是可留到部署阶段的普通残余风险。blocked candidate commit 不代表批准或可合并。
3. `funding=included` 因 result.v2 无 funding ledger，按冻结合同返回 unsupported。
4. paper runner 未部署；本地 `paper_tick` 只证明与 replay 共用同一 `evaluate` 及 ledger，不证明 62-3 runtime。
5. 上线前仍需 immutable revision 下的真实 `/health`、`/catalog`、artifact request、Connector callback/readback 和 Pre 证据。
