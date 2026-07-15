# 62-2a Provider compiler / kernel / capability 复核记录

日期：2026-07-15
分支：`codex/62-2a-provider-kernel`
基线：`8cfe18e5fd7d82c50f7f5079e4171b4e333352dd`

## 结论

本分支已实现 62-2a 的 Provider 本地基础：完整 StrategySpec v2/manifest 静态编译、Provider-local capability/hash、artifact execution wire 校验、历史 replay 与 future paper 共用的单一 `StrategyKernel.evaluate`、result.v2/trace/evidence 构造，以及 legacy 七个固定策略的兼容路由。

当前结论为 **Provider-local Source + Local tests Pass，等待独立只读复验与串行合入**。Owner 已决定 62-2a 暂不声明三种 synthetic transform，只支持 exact complete streams；Provider producer、fixture/hash 和 compile/API fail-closed 测试已按该决定收窄。本分支没有部署 Provider、没有接触 Pre、Fan、RN 或真实中心数据，因此这些证据仍为 **Blocked / Cannot assess**，不能由本地通过替代；TokenBeep 共享 fixture、Connector 和 Server 也尚未同步，不能把本记录解释为跨仓合同已经对齐。

## 冻结合同与实现边界

- 只读对照 TokenBeep 的 `SPEC_策略工件与统一执行契约.md`、`IMPL_策略接入与不可变版本.md` 和共享 capability fixture；冻结 SPEC 未修改。
- Provider 本地 fixture 保留 `binance_futures` 等 62-2a exact-set，但 `data_transforms=[]`，新 SHA-256 为 `ab659c29d5feb6f0691a1ef1a1a7a0b9db71619279ac4a397bd9b6c2a0e5f00a`。该 hash 仅代表 Provider-local payload；TokenBeep 共享 fixture 仍需后续串行生成并同步其消费者。
- 只有 `local.strategy_spec_v2.compiler` 在 immutable lowercase revision 下出现并声明 capability；七个 legacy tool 不声明 artifact capability。revision 未锁定时 compiler tool 不进入 catalog。
- artifact-shaped 或 partial artifact 请求一律 fail-closed，不回退 fixed tool、ccxt 或默认策略。
- 当前 Provider capability 是 exact subset（binary `add`、`gt`、`crosses_above`）。编译器和 kernel 已实现 `add/mul/min/max` 变参 `>=2`、`sub/div/compare/cross` 固定二元、`abs` 固定一元；只有 capability 精确出现实际 `arg_types` 长度的签名时才可执行，不从二元签名推断三元签名。

## Owner 决策与本次增量

- Owner 决定：62-2a 暂不声明 `combine_first.v1`、`ffill_after_close.v1`、`flow_dilution_shifted.v1`；62-2b 真正实现并验证零前视语义后，才能升级 fixture/hash 恢复声明。
- `capability_payload()` 与 Provider fixture 都明确输出 `data_transforms=[]`；catalog producer、execution validator 和 evidence 继续复用同一 payload/hash，没有另一个隐式广告源。
- `data_requirements[*].allowed_transforms` 仍按冻结 manifest 结构投影到 `capability_requirements.data_transforms`。三项 canonical transform 名称可被 manifest parser 识别，但因不在 Provider capability 中，会在 `compile_strategy()` 的 capability membership 检查稳定返回 `ERR_STRATEGY_SPEC_UNSUPPORTED`；HTTP 路径同样在任何中心数据访问前失败关闭。
- empty transforms 的 exact-complete-stream artifact 继续通过；本次没有加入 transform dispatcher、synthetic frame builder、coverage transform 记录或任何 62-2b/62-2c 生产逻辑。

## 本地实现检查

- StrategySpec/manifest 顶层与嵌套 exact-key、排序、静态类型、hash/binding、data requirement 和 source material 校验。
- capability 顶层/嵌套 exact-key、16 KiB、数组上限、排序去重、静态类型与真实 operator arity 校验。
- Decimal128 precision 34 / ROUND_HALF_EVEN；integer safe range；next-open、tick/qty、long/short gap、same-bar priority、三种 sizing、双边 fee/slippage、time/signal/end-of-data 语义。
- closed FeatureFrame、rolling_sum、lag/cross 防前视；同一 source/interval 可绑定多个 feature，不互相覆盖。
- execution request 的 11-key exact-set、Snowflake string、artifact/spec/manifest/capability/revision/result-contract 绑定；Server 注入的 trusted `instrument_rules` exact object。
- result.v2 五键、trade 十键、coverage/trace/evidence 和各 canonical hash；中心数据声明不匹配、空流、gap、未覆盖完整对齐区间均失败关闭。
- validator 仅对白名单字段放行 7..64 lowercase `provider_revision` 与 64 lowercase `strategy_execution_capability_hash`；同形状值放在无关字段仍被 secret scan 拒绝。
- 已搜索 Provider repo 内 `capability_payload`、`capability_hash`、catalog `strategy_execution_capability*`、execution `expected_capability_hash`、fixture、focused tests 与 review 的全部使用点；producer 与 Provider-local fixture/hash 同步收窄，未发现第二套 transform capability producer。

## 验证证据

| 层级 | 结果 | 命令 / 证据 |
|---|---|---|
| Focused compiler/kernel/API | Pass | `python3 -m pytest -q backtesting-py/tests/test_strategy_kernel.py` → `37 passed`；三项未实现 transform 分别验证 compile 与 HTTP capability 阶段拒绝，数据抓取探针未触发；既有 exact-complete-stream replay、paper tick、Decimal、exit/sizing、result.v2、trace/evidence 与 replay-frame conformance 同套通过 |
| Provider + validator + installer tests | Pass | `PYTHONPATH=validator python3 -m pytest -q backtesting-py/tests validator/tests scripts/tests --import-mode=importlib` → `158 passed` |
| Format | Pass | `python3 -m black --check backtesting-py/strategy_kernel.py backtesting-py/tests/test_strategy_kernel.py`；isort `--check-only --profile black` 同范围 |
| Lint | Pass | `python3 -m flake8 --extend-ignore=E501 backtesting-py/strategy_kernel.py backtesting-py/tests/test_strategy_kernel.py`；保留 Black 88 列口径 |
| Diff hygiene | Pass | `git diff --check`；提交前另跑 `git diff --cached --check` |
| Pre / deployed Provider | Blocked | 未部署、未读取节点 revision、未调用真实 catalog/backtest |
| 中心数据 / transform | Cannot assess | 未调用真实 Binance Vision/中心 K 线/CoinGlass；无 Provider runtime evidence |
| Fan / RN | Cannot assess | 不在本分支执行范围，未接触设备 |

测试仅出现 FastAPI `on_event` deprecation、macOS LibreSSL/urllib3 三条既有 warning，不改变断言结果。

## 残余风险与后续边界

1. 62-2b 的 coverage 目录、真实 freshness/gap incident、`combine_first.v1`、`ffill_after_close.v1`、`flow_dilution_shifted.v1` 及零前视 fixtures 尚未实现。本分支只允许 exact complete streams；任何上述 transform 请求都在 capability 阶段 fail-closed，不能视为 62-2b 完成。
2. TokenBeep 共享 capability fixture、Connector 与 Server 仍需由编排器后续串行同步并分别验证；在此之前可能存在旧 hash/旧 transform 列表的跨仓不一致。本 Provider-local commit 不代表共享 fixture 已更新，也不代表跨仓验收完成。
3. `funding=included` 因 result.v2 无 funding ledger，按冻结合同返回 unsupported。
4. paper runner 未部署；本地 `paper_tick` 只证明与 replay 共用同一 `evaluate` 及 ledger，不证明 62-3 runtime。
5. 上线前仍需 immutable revision 下的真实 `/health`、`/catalog`、artifact request、Connector callback/readback 和 Pre 证据。
